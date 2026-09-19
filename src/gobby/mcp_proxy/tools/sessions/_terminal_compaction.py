"""The handoff compaction sequence: interrupt, drain, submit, verify, watch.

Backend-neutral by construction — every write and read goes through the ``PaneIO``
protocol, so the same sequence drives a native (gterm) pane and a tmux one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, ComposerRead, IdleDetector
from gobby.terminals.pane_io import PaneIO, SendResult, clear_composer
from gobby.terminals.runtime import NamedKey

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

ComposerReader = Callable[[str | None], ComposerRead]

logger = logging.getLogger(__name__)

# Maps session.source (CLI provider name) to the slash command that triggers
# context compaction in the running CLI subprocess.
_CLI_COMPACT_COMMANDS: dict[str, str] = {
    "claude": "/compact",
    "codex": "/compact",
    "grok": "/compact",
    "qwen": "/compress",
    "droid": "/compress",
}
_DEFAULT_COMPACT_INTERRUPT_KEY: NamedKey = "escape"
_CLI_COMPACT_INTERRUPT_KEYS: dict[str, NamedKey] = {
    "codex": "ctrl_c",
    # Grok 1.0.30: Esc never cancels a turn; Ctrl+C on an empty composer does.
    "grok": "ctrl_c",
}
# Blind path (no transcript observer): wait this long after the interrupt key.
_DEFAULT_INTERRUPT_SETTLE_SECONDS = 0.1
# Observed path: poll the transcript this long per interrupt attempt.
_OBSERVED_INTERRUPT_SETTLE_SECONDS = 1.0
_INTERRUPT_ATTEMPTS = 3
_INTERRUPT_POLL_SECONDS = 0.05
# After submitting the command, poll the pane this long for the CLI rejecting it
# because its turn is still running; a rejection interrupts again and resubmits.
_COMPACTION_REJECTION_SETTLE_SECONDS = 1.0
_COMPACTION_REJECTION_POLL_SECONDS = 0.1
# When a turn_settled observer exists, poll it this long before the first interrupt.
_TURN_SETTLE_WAIT_SECONDS = 30.0
_TURN_SETTLE_POLL_SECONDS = 0.25
# Droid 0.219.0 answers a submitted /compress (never /clear) with a "Confirm /compress"
# modal that waits for Enter; poll the pane this long for it before watching for a rejection.
_CLI_COMPACT_CONFIRM_PROMPTS: dict[tuple[str, str], str] = {
    ("droid", "/compress"): "Confirm /compress",
}
_COMPACTION_CONFIRM_SETTLE_SECONDS = 5.0
_COMPACTION_REJECTION_RETRIES = 1
_COMPACTION_REJECTION_CAPTURE_LINES = 30
# The typed command and its Enter are two writes, and a CLI still redrawing an
# interrupted turn reads them as one input chunk and takes the Enter as a literal
# newline. Both writes still report Delivered, so the composer is read back: poll it
# this long for the command to leave, and drain/retype this many times before failing.
_SUBMIT_VERIFY_SETTLE_SECONDS = 2.0
_SUBMIT_VERIFY_POLL_SECONDS = 0.1
_SUBMIT_RETRIES = 1
_COMPACTION_REJECTION_ERROR_CODE = "compaction_command_rejected"
_COMMAND_NOT_SUBMITTED_ERROR_CODE = "command_not_submitted"
_COMPOSER_NOT_CLEAN_ERROR_CODE = "composer_not_clean"
_COMPOSER_OCCUPIED_ERROR_CODE = "composer_occupied"
_INTERRUPT_UNCONFIRMED_ERROR_CODE = "interrupt_unconfirmed"
_INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE = "interrupt_observation_unavailable"


def composer_reader(db: HubDatabase, cli_source: str | None) -> ComposerReader | None:
    """Bind the provider's composer probe for a compaction, or None without a provider."""
    if not cli_source:
        return None
    return IdleDetector(DetectionManifestRegistry(db), cli_source).composer_read


def _compact_interrupt_key(source: str | None) -> NamedKey:
    if source is None:
        return _DEFAULT_COMPACT_INTERRUPT_KEY
    return _CLI_COMPACT_INTERRUPT_KEYS.get(source, _DEFAULT_COMPACT_INTERRUPT_KEY)


def _fresh_output_delta(before: str, after: str) -> str:
    if not before:
        return after
    if after.startswith(before):
        return after[len(before) :]

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    max_overlap = min(len(before_lines), len(after_lines))
    for overlap in range(max_overlap, 0, -1):
        if before_lines[-overlap:] == after_lines[:overlap]:
            return "\n".join(after_lines[overlap:])
    return ""


async def _capture_pane_snapshot(
    pane: PaneIO,
    *,
    lines: int = _COMPACTION_REJECTION_CAPTURE_LINES,
) -> str | None:
    return await pane.snapshot(lines)


def _detect_compaction_rejection(
    before: str | None,
    after: str | None,
    command: str,
) -> dict[str, str] | None:
    if before is None or after is None:
        return None

    rejection_message = f"'{command}' is disabled while a task is in progress"
    if rejection_message not in _fresh_output_delta(before, after):
        return None
    return {
        "error_code": _COMPACTION_REJECTION_ERROR_CODE,
        "rejected_command": command,
        "rejection_message": rejection_message,
    }


def _log_pane_failure(pane: PaneIO, session_id: str, action: str, reason: str | None) -> None:
    logger.warning(
        "Failed %s on %s target %s for session %s: %s",
        action,
        pane.backend,
        pane.target,
        session_id,
        reason,
        extra={
            "event": "terminal_key_delivery_failed",
            "action": action,
            "backend": pane.backend,
            "target": pane.target,
            "session_id": session_id,
        },
    )


async def _send_pane_key(
    pane: PaneIO,
    key: NamedKey,
    session_id: str,
    *,
    action: str,
) -> tuple[bool, str | None]:
    """Send one named key and keep failures structured for MCP callers."""
    ok, reason = await pane.send_key(key)
    if not ok:
        _log_pane_failure(pane, session_id, action, reason)
        return False, f"{reason} (session {session_id} while {action})"
    return True, None


async def _wait_for_interrupt(
    observe_interrupt: Callable[[], bool | None],
    *,
    attempt_seconds: float,
    poll_seconds: float = _INTERRUPT_POLL_SECONDS,
) -> bool | None:
    """Poll the transcript observer for a fresh interrupt during one attempt."""
    if attempt_seconds <= 0:
        return observe_interrupt()

    elapsed = 0.0
    while elapsed < attempt_seconds:
        observed = observe_interrupt()
        if observed is not False:
            return observed
        delay = min(poll_seconds, attempt_seconds - elapsed)
        await asyncio.sleep(delay)
        elapsed += delay
    return observe_interrupt()


async def _confirm_interrupt(
    pane: PaneIO,
    key: NamedKey,
    session_id: str,
    observe_interrupt: Callable[[], bool | None],
    *,
    attempt_seconds: float,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Send the interrupt key until the CLI's transcript confirms the turn stopped."""
    for _attempt in range(_INTERRUPT_ATTEMPTS):
        ok, reason = await _send_pane_key(
            pane, key, session_id, action="sending compaction interrupt"
        )
        if not ok:
            return False, reason, None
        observed = await _wait_for_interrupt(observe_interrupt, attempt_seconds=attempt_seconds)
        if observed is None:
            return (
                False,
                "transcript became unavailable during interrupt confirmation",
                {
                    "error_code": _INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE,
                    "continuation_pending": False,
                },
            )
        if observed:
            return True, None, None
    return (
        False,
        f"CLI did not confirm interruption after {_INTERRUPT_ATTEMPTS} attempts",
        {
            "error_code": _INTERRUPT_UNCONFIRMED_ERROR_CODE,
            "continuation_pending": False,
        },
    )


async def _command_left_composer(
    pane: PaneIO,
    command: str,
    composer_read: ComposerReader,
    *,
    window_seconds: float,
    poll_seconds: float = _SUBMIT_VERIFY_POLL_SECONDS,
) -> bool:
    """Poll the composer until it stops holding ``command``.

    Only a positive draft whose row starts with the command proves the CLI never
    took it. ``empty`` says the Enter landed, and ``unknown`` — no frame, no
    snapshot, a redraw the manifest cannot classify — proves nothing either way, so
    it passes rather than report a delivered compaction as a failure.
    """
    elapsed = 0.0
    while True:
        read = composer_read(await pane.snapshot(COMPOSER_PROBE_LINES, mode="ansi"))
        if read.state != "draft" or not read.line.startswith(command):
            return True
        if elapsed >= window_seconds:
            return False
        delay = min(poll_seconds, window_seconds - elapsed)
        await asyncio.sleep(delay)
        elapsed += delay


async def _submit_command(
    pane: PaneIO,
    command: str,
    session_id: str,
    *,
    cli_source: str | None,
    composer_read: ComposerReader | None,
    verify_seconds: float,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Type ``command`` into the drained composer and press Enter until it submits.

    A Delivered Enter is not a submitted command: the CLI can take it as a literal
    newline and leave the command on screen. So the composer is read back, and a
    command still sitting there is drained and retyped — the reads that detect the
    failure are also what space the retry's writes apart — before the delivery fails
    with ``command_not_submitted``. Without a ``composer_read`` (no provider) the
    write outcome stays the only available evidence.
    """
    for attempt in range(1 + _SUBMIT_RETRIES):
        if attempt:
            logger.warning(
                "Session %s still held %s in its composer after Enter; "
                "draining and retyping (attempt %d of %d)",
                session_id,
                command,
                attempt + 1,
                1 + _SUBMIT_RETRIES,
            )
            cleared, clear_reason = await clear_composer(pane, cli_source)
            if not cleared:
                _log_pane_failure(pane, session_id, "clearing the composer", clear_reason)
                return (
                    False,
                    f"composer could not be cleared before {command}: {clear_reason}",
                    {"error_code": _COMPOSER_NOT_CLEAN_ERROR_CODE, "continuation_pending": False},
                )
        ok, reason = await pane.type_text(command)
        if not ok:
            _log_pane_failure(pane, session_id, "typing compaction command", reason)
            return False, reason, None
        ok, reason = await _send_pane_key(
            pane, "enter", session_id, action="submitting compaction command"
        )
        if not ok:
            return False, reason, None
        if composer_read is None:
            return True, None, None
        if await _command_left_composer(
            pane, command, composer_read, window_seconds=verify_seconds
        ):
            return True, None, None
    return (
        False,
        f"{command} was typed but stayed in the composer: the CLI never submitted it",
        {"error_code": _COMMAND_NOT_SUBMITTED_ERROR_CODE, "continuation_pending": False},
    )


async def _interrupt_turn(
    pane: PaneIO,
    key: NamedKey,
    session_id: str,
    observe_interrupt: Callable[[], bool | None] | None,
    *,
    settle_seconds: float,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Interrupt the running turn: transcript-confirmed, or blind with a settle."""
    if observe_interrupt is not None:
        return await _confirm_interrupt(
            pane, key, session_id, observe_interrupt, attempt_seconds=settle_seconds
        )
    ok, reason = await _send_pane_key(pane, key, session_id, action="sending compaction interrupt")
    if not ok:
        return False, reason, None
    if settle_seconds > 0:
        await asyncio.sleep(settle_seconds)
    return True, None, None


def _turn_already_settled(
    turn_settled: Callable[[], bool | None] | None,
    session_id: str,
    command: str,
) -> bool:
    """Return whether the CLI's transcript shows no running turn, so no interrupt is sent."""
    if turn_settled is None or turn_settled() is not True:
        return False
    logger.info(
        "Session %s has no running turn; submitting %s without an interrupt",
        session_id,
        command,
    )
    return True


def _turn_settle_wait_budget(settle_seconds: float | None) -> tuple[float, float]:
    """Return the settle wait and poll interval, honoring the test override."""
    if settle_seconds is None:
        return _TURN_SETTLE_WAIT_SECONDS, _TURN_SETTLE_POLL_SECONDS
    if settle_seconds <= 0:
        return 0.0, 0.0
    return settle_seconds, min(_TURN_SETTLE_POLL_SECONDS, settle_seconds)


async def _wait_for_turn_to_settle(
    turn_settled: Callable[[], bool | None] | None,
    session_id: str,
    command: str,
    *,
    wait_seconds: float,
    poll_seconds: float,
) -> bool:
    """Poll ``turn_settled`` until the CLI turn ends or the bounded wait expires."""
    if turn_settled is None:
        return False
    deadline = time.monotonic() + wait_seconds
    while True:
        if _turn_already_settled(turn_settled, session_id, command):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_seconds, remaining) if poll_seconds > 0 else remaining)


async def _confirm_compaction_prompt(
    pane: PaneIO,
    before_command: str | None,
    command: str,
    cli_source: str | None,
    session_id: str,
    *,
    window_seconds: float,
    poll_seconds: float = _COMPACTION_REJECTION_POLL_SECONDS,
) -> SendResult:
    """Press Enter on the CLI's compaction confirm modal once it appears in the pane."""
    prompt = _CLI_COMPACT_CONFIRM_PROMPTS.get((cli_source or "", command))
    if prompt is None or before_command is None or prompt in before_command:
        return True, None
    elapsed = 0.0
    while True:
        # The modal redraws the TUI rather than appending output, so match the screen.
        snapshot = await _capture_pane_snapshot(pane)
        if snapshot is not None and prompt in snapshot:
            return await _send_pane_key(
                pane, "enter", session_id, action="confirming compaction command"
            )
        if elapsed >= window_seconds:
            logger.warning(
                "Session %s never showed %r after its compaction command",
                session_id,
                prompt,
            )
            return True, None
        delay = min(poll_seconds, window_seconds - elapsed)
        await asyncio.sleep(delay)
        elapsed += delay


async def _wait_for_compaction_rejection(
    pane: PaneIO,
    before_command: str | None,
    command: str,
    *,
    window_seconds: float,
    poll_seconds: float = _COMPACTION_REJECTION_POLL_SECONDS,
) -> dict[str, str] | None:
    """Poll the pane for the CLI rejecting ``command`` until the window elapses."""
    elapsed = 0.0
    while True:
        delay = min(poll_seconds, window_seconds - elapsed)
        if delay > 0:
            await asyncio.sleep(delay)
            elapsed += delay
        rejection = _detect_compaction_rejection(
            before_command, await _capture_pane_snapshot(pane), command
        )
        if rejection is not None or elapsed >= window_seconds:
            return rejection


async def _send_terminal_compaction_command(
    pane: PaneIO,
    command: str,
    session_id: str,
    *,
    cli_source: str | None,
    mark_continuation_pending: Callable[[], bool],
    clear_continuation_pending: Callable[[], bool],
    schedule_continuation_readiness: Callable[[str | None], bool] | None = None,
    continuation_readiness_capture_lines: int | None = None,
    observe_interrupt: Callable[[], bool | None] | None = None,
    turn_settled: Callable[[], bool | None] | None = None,
    settle_seconds: float | None = None,
    interrupt_settle_seconds: float = _DEFAULT_INTERRUPT_SETTLE_SECONDS,
    rejection_settle_seconds: float = _COMPACTION_REJECTION_SETTLE_SECONDS,
    composer_read: ComposerReader | None = None,
) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
    """Interrupt a live turn, drain the composer, submit the command, watch for a rejection.

    ``settle_seconds`` overrides every wait (tests); ``observe_interrupt`` is the
    transcript observer for CLIs that record interrupts, and its absence keeps the
    blind interrupt path for CLIs that do not. ``turn_settled`` reports whether the
    CLI's own transcript shows its last turn ended: a live turn is polled until it
    settles or the bounded wait expires. A settled turn is never interrupted
    (Ctrl+C on an idle Codex or Grok composer quits or escalates toward quit), so
    the command is submitted directly and the success detail carries
    ``interrupted: False``. Interrupt only on timeout. A CLI that rejects the
    command because its turn is still running (Grok) is interrupted again and the
    command resubmitted once before the delivery fails. ``composer_read`` probes
    the composer first: a positive operator draft refuses the whole delivery with
    ``composer_occupied`` before any key is sent, so the operator's draft and the
    live turn are both left alone; the agent retries once the draft is submitted.
    It then reads the composer back after Enter, so a command the CLI typed but
    never submitted fails with ``command_not_submitted`` instead of reporting
    success on the strength of the write outcome.
    """
    continuation_pending = False
    if composer_read is not None:
        read = composer_read(await pane.snapshot(COMPOSER_PROBE_LINES, mode="ansi"))
        if read.state == "draft":
            logger.info(
                "Refusing %s for session %s: composer holds an operator draft",
                command,
                session_id,
            )
            return (
                False,
                "composer holds an operator draft",
                False,
                {"error_code": _COMPOSER_OCCUPIED_ERROR_CODE, "continuation_pending": False},
            )
    interrupt_key = _compact_interrupt_key(cli_source)
    interrupt_seconds = interrupt_settle_seconds if settle_seconds is None else settle_seconds
    rejection_seconds = rejection_settle_seconds if settle_seconds is None else settle_seconds
    confirm_seconds = (
        _COMPACTION_CONFIRM_SETTLE_SECONDS if settle_seconds is None else settle_seconds
    )
    verify_seconds = _SUBMIT_VERIFY_SETTLE_SECONDS if settle_seconds is None else settle_seconds
    settle_wait_seconds, settle_poll_seconds = _turn_settle_wait_budget(settle_seconds)
    if observe_interrupt is not None:
        continuation_pending = bool(mark_continuation_pending())
        if not continuation_pending:
            return (
                False,
                "failed to persist handoff continuation before compaction",
                False,
                None,
            )

    readiness_before_command: str | None = None
    rejection: dict[str, str] | None = None
    interrupt_sent = False
    for resubmission in range(1 + _COMPACTION_REJECTION_RETRIES):
        if resubmission:
            logger.warning(
                "Session %s rejected %s while its task was still running; "
                "interrupting again before resubmission %d of %d",
                session_id,
                command,
                resubmission,
                _COMPACTION_REJECTION_RETRIES,
            )
        # A rejection proves the turn is live, so only the first submission may skip.
        if resubmission or not await _wait_for_turn_to_settle(
            turn_settled,
            session_id,
            command,
            wait_seconds=settle_wait_seconds,
            poll_seconds=settle_poll_seconds,
        ):
            interrupted, reason, detail = await _interrupt_turn(
                pane,
                interrupt_key,
                session_id,
                observe_interrupt,
                settle_seconds=interrupt_seconds,
            )
            if not interrupted:
                if continuation_pending:
                    clear_continuation_pending()
                return False, reason, False, detail
            interrupt_sent = True

        before_command = await _capture_pane_snapshot(pane)
        readiness_before_command = before_command
        if (
            schedule_continuation_readiness is not None
            and continuation_readiness_capture_lines is not None
        ):
            readiness_before_command = await _capture_pane_snapshot(
                pane,
                lines=continuation_readiness_capture_lines,
            )
        if observe_interrupt is None and not continuation_pending:
            continuation_pending = bool(mark_continuation_pending())
        if schedule_continuation_readiness is not None and not continuation_pending:
            return (
                False,
                "failed to persist handoff continuation before compaction",
                False,
                None,
            )

        cleared, clear_reason = await clear_composer(pane, cli_source)
        if not cleared:
            if continuation_pending:
                clear_continuation_pending()
            _log_pane_failure(pane, session_id, "clearing the composer", clear_reason)
            return (
                False,
                f"composer could not be cleared before {command}: {clear_reason}",
                False,
                {"error_code": _COMPOSER_NOT_CLEAN_ERROR_CODE, "continuation_pending": False},
            )

        ok, reason, submit_detail = await _submit_command(
            pane,
            command,
            session_id,
            cli_source=cli_source,
            composer_read=composer_read,
            verify_seconds=verify_seconds,
        )
        if ok:
            submit_detail = None
            ok, reason = await _confirm_compaction_prompt(
                pane,
                before_command,
                command,
                cli_source,
                session_id,
                window_seconds=confirm_seconds,
            )
        if not ok:
            if continuation_pending:
                clear_continuation_pending()
            return False, reason, False, submit_detail

        rejection = await _wait_for_compaction_rejection(
            pane, before_command, command, window_seconds=rejection_seconds
        )
        if rejection is None:
            break

    if rejection is not None:
        if continuation_pending:
            clear_continuation_pending()
        return False, rejection["rejection_message"], False, rejection
    if schedule_continuation_readiness is not None and not schedule_continuation_readiness(
        readiness_before_command
    ):
        logger.warning(
            "Failed to schedule handoff continuation readiness for session %s; "
            "SessionStart fallback remains pending",
            session_id,
        )
    return True, None, continuation_pending, None if interrupt_sent else {"interrupted": False}
