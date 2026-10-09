"""The handoff compaction sequence: interrupt, drain, submit, verify, watch.

Backend-neutral by construction — every write and read goes through the ``PaneIO``
protocol, so the same sequence drives a native (gterm) pane and a tmux one.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.idle_detector import IdleDetector
from gobby.terminals.composer_ledger import record_composer_submit
from gobby.terminals.composer_lock import composer_action_lock
from gobby.terminals.pane_io import (
    DEFAULT_SNAPSHOT_LINES,
    ENTER_DELIVERY_UNCONFIRMED_ERROR_CODE,
    SUBMIT_UNVERIFIED_ERROR_CODE,
    SUBMIT_VERIFY_SECONDS,
    ComposerReader,
    DrainResult,
    PaneIO,
    SendResult,
    clear_composer,
    composer_gate_for_write,
    log_pane_failure,
    send_pane_key,
    submit_text,
)
from gobby.terminals.runtime import NamedKey, SnapshotMode

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

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
    # Codex 0.159.0: idle Ctrl+C requests quit; Escape interrupts only running work.
    # openai/codex rust-v0.159.0, codex-rs/tui/src/: interaction.rs:510-521
    # (under chatwidget/), keymap.rs:1603, bottom_pane/mod.rs:1598-1611.
    "codex": "escape",
    # Grok 1.0.30: Esc never cancels a turn; Ctrl+C on an empty composer does.
    "grok": "ctrl_c",
}
# Blind path (no transcript observer): wait this long after the interrupt key.
_DEFAULT_INTERRUPT_SETTLE_SECONDS = 0.1
# Observed path: poll the transcript this long per interrupt attempt.
# The failed Grok goal-mode attempt restarted its model loop about 9 seconds
# after the last Ctrl+C. Keep observing before another potentially quitting press.
_OBSERVED_INTERRUPT_SETTLE_SECONDS = 12.0
_INTERRUPT_ATTEMPTS = 3
# Codex takes a Ctrl+C on an idle composer as a step toward quitting, so a press
# that lands just after its turn ends can make the next one exit the CLI. It gets
# a single press observed across the whole window (#23095).
_CLI_INTERRUPT_PRESSES: dict[str, int] = {"codex": 1}
_INTERRUPT_POLL_SECONDS = 0.05
# After submitting the command, poll the pane this long for the CLI rejecting it
# because its turn is still running; a rejected /clear interrupts again and resubmits.
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
# The command and its newline are one write and the Enter follows as its own; both
# report Delivered whether or not the CLI took them, so `submit_text` reads the
# composer back and polls it this long for the command to leave before its next rung.
_SUBMIT_VERIFY_SETTLE_SECONDS = SUBMIT_VERIFY_SECONDS
_COMPACTION_REJECTION_ERROR_CODE = "compaction_command_rejected"
_COMMAND_NOT_SUBMITTED_ERROR_CODE = "command_not_submitted"
_COMPOSER_NOT_CLEAN_ERROR_CODE = "composer_not_clean"
_COMPOSER_OCCUPIED_ERROR_CODE = "composer_occupied"
_COMPOSER_UNKNOWN_ERROR_CODE = "composer_unknown"
_INTERRUPT_UNCONFIRMED_ERROR_CODE = "interrupt_unconfirmed"
# The session's recorded CLI process no longer owns its pane, so no key may be sent.
NO_TERMINAL_TARGET_ERROR_CODE = "no_terminal_target"
_SEAT_LEFT_REASON = "the recorded CLI process no longer owns its terminal"
_INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE = "interrupt_observation_unavailable"


class _SeatLeftError(RuntimeError):
    """The recorded CLI left its pane before a key or text write."""


class _SeatGuardedPane:
    """PaneIO that checks seat ownership immediately before every write.

    A delivery can wait tens of seconds between its first ownership check and a
    key: the turn-settle wait, each interrupt observation, the submit ladder's
    Enter retries. A CLI that exits meanwhile leaves its shell or a successor in
    the pane, so each write re-checks instead of trusting an earlier answer.
    """

    def __init__(self, pane: PaneIO, seat_left: Callable[[], bool]) -> None:
        self._pane = pane
        self._seat_left = seat_left

    @property
    def backend(self) -> str:
        return self._pane.backend

    @property
    def target(self) -> str:
        return self._pane.target

    async def send_key(self, key: NamedKey) -> SendResult:
        await self.require_seat()
        return await self._pane.send_key(key)

    async def type_text(self, text: str) -> SendResult:
        await self.require_seat()
        return await self._pane.type_text(text)

    async def snapshot(
        self, lines: int = DEFAULT_SNAPSHOT_LINES, *, mode: SnapshotMode = "text"
    ) -> str | None:
        return await self._pane.snapshot(lines, mode=mode)

    async def require_seat(self) -> None:
        if await asyncio.to_thread(self._seat_left):
            raise _SeatLeftError(_SEAT_LEFT_REASON)


def composer_reader(db: HubDatabase, cli_source: str | None) -> ComposerReader | None:
    """Bind the provider's composer probe for a compaction, or None when it cannot read one."""
    if not cli_source:
        return None
    detector = IdleDetector(DetectionManifestRegistry(db), cli_source)
    return detector.composer_read if detector.reads_composer() else None


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


def compaction_refusal(output: str, command: str) -> str | None:
    """Return the CLI's refusal of ``command`` while its turn runs, quoted or not."""
    match = re.search(rf"'?{re.escape(command)}'? is disabled while a task is in progress", output)
    return match.group(0) if match else None


def _detect_compaction_rejection(
    before: str | None,
    after: str | None,
    command: str,
) -> dict[str, str] | None:
    if before is None or after is None:
        return None

    rejection_message = compaction_refusal(_fresh_output_delta(before, after), command)
    if rejection_message is None:
        return None
    return {
        "error_code": _COMPACTION_REJECTION_ERROR_CODE,
        "rejected_command": command,
        "rejection_message": rejection_message,
    }


async def _wait_for_interrupt(
    observe_interrupt: Callable[[], bool | None],
    *,
    attempt_seconds: float,
    poll_seconds: float = _INTERRUPT_POLL_SECONDS,
) -> bool | None:
    """Poll the transcript observer for a fresh interrupt during one attempt."""
    if attempt_seconds <= 0:
        return await asyncio.to_thread(observe_interrupt)

    elapsed = 0.0
    while elapsed < attempt_seconds:
        observed = await asyncio.to_thread(observe_interrupt)
        if observed is not False:
            return observed
        delay = min(poll_seconds, attempt_seconds - elapsed)
        await asyncio.sleep(delay)
        elapsed += delay
    return await asyncio.to_thread(observe_interrupt)


async def _send_compaction_interrupt(
    pane: PaneIO,
    key: NamedKey,
    session_id: str,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Recheck the composer immediately before each interrupt, including retries."""
    writable, refuse_reason, state = await composer_gate_for_write(
        pane, action="a compaction interrupt"
    )
    if not writable:
        return (
            False,
            refuse_reason,
            {
                "error_code": (
                    _COMPOSER_OCCUPIED_ERROR_CODE
                    if state == "draft"
                    else _COMPOSER_UNKNOWN_ERROR_CODE
                ),
                "continuation_pending": False,
            },
        )
    ok, reason = await send_pane_key(pane, key, session_id, action="sending compaction interrupt")
    return ok, reason, None


async def _confirm_interrupt(
    pane: PaneIO,
    key: NamedKey,
    session_id: str,
    observe_interrupt: Callable[[], bool | None],
    *,
    attempt_seconds: float,
    turn_settled: Callable[[], bool | None] | None = None,
    presses: int = _INTERRUPT_ATTEMPTS,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Send the interrupt key until the CLI's transcript confirms the turn stopped."""
    pressed = False
    for attempt in range(presses + 1):
        if pressed and turn_settled is not None and (await asyncio.to_thread(turn_settled)) is True:
            # Grok goal mode can start a successor about 92 ms after a completed
            # turn. Confirm that the composer stays idle before treating it as
            # the interrupt result or sending another Ctrl+C.
            await asyncio.sleep(_TURN_SETTLE_POLL_SECONDS)
            if (await asyncio.to_thread(turn_settled)) is True:
                return True, None, None
        if attempt == presses:
            break
        ok, reason, detail = await _send_compaction_interrupt(pane, key, session_id)
        if not ok:
            return False, reason, detail
        pressed = True
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
        f"CLI did not confirm interruption after {presses} attempts",
        {
            "error_code": _INTERRUPT_UNCONFIRMED_ERROR_CODE,
            "continuation_pending": False,
        },
    )


async def _submit_command(
    pane: PaneIO,
    command: str,
    session_id: str,
    *,
    cli_source: str | None,
    composer_read: ComposerReader | None,
    verify_seconds: float,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Submit ``command`` through the shared verified-submit ladder.

    Both halves of one handoff -- this command and the pull prompt that follows the
    compaction -- run the same ladder, so they cannot drift apart.
    """
    result = await submit_text(
        pane,
        command,
        session_id,
        label=command,
        cli_source=cli_source,
        composer_read=composer_read,
        verify_seconds=verify_seconds,
        pending_payload=command,
    )
    if result.error_code == _COMMAND_NOT_SUBMITTED_ERROR_CODE:
        logger.error(
            "Session %s did not submit the set_handoff compact command %s: %s",
            session_id,
            command,
            result.reason,
            extra={
                "event": "handoff_continuation_not_submitted",
                "session_id": session_id,
                "error_code": result.error_code,
            },
        )
    if result.ok or result.error_code is None:
        return result.ok, result.reason, None
    return (
        False,
        result.reason,
        {"error_code": result.error_code, "continuation_pending": False},
    )


async def _interrupt_turn(
    pane: PaneIO,
    key: NamedKey,
    session_id: str,
    observe_interrupt: Callable[[], bool | None] | None,
    *,
    settle_seconds: float,
    turn_settled: Callable[[], bool | None] | None = None,
    presses: int = _INTERRUPT_ATTEMPTS,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Interrupt the running turn: transcript-confirmed, or blind with a settle."""
    if observe_interrupt is not None:
        return await _confirm_interrupt(
            pane,
            key,
            session_id,
            observe_interrupt,
            # Fewer presses keep the whole observation window.
            attempt_seconds=settle_seconds * _INTERRUPT_ATTEMPTS / presses,
            turn_settled=turn_settled,
            presses=presses,
        )
    ok, reason, detail = await _send_compaction_interrupt(pane, key, session_id)
    if not ok:
        return False, reason, detail
    if settle_seconds > 0:
        await asyncio.sleep(settle_seconds)
    return True, None, None


async def _turn_already_settled(
    turn_settled: Callable[[], bool | None] | None,
    session_id: str,
    command: str,
) -> bool:
    """Return whether the CLI's transcript shows no running turn, so no interrupt is sent."""
    if turn_settled is None or (await asyncio.to_thread(turn_settled)) is not True:
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
        if await _turn_already_settled(turn_settled, session_id, command):
            return True
        recorded = getattr(turn_settled, "delivery_turn_recorded", None)
        if callable(recorded) and await asyncio.to_thread(recorded):
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
            return await send_pane_key(
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
    on_command_submitting: Callable[[], None] | None = None,
    seat_left: Callable[[], bool] | None = None,
) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
    """Serialize the whole compaction ladder under the shared composer lock.

    The composer probe, interrupt, drain, submit and verify are all separate
    awaits against one physical composer, so a concurrent wake would otherwise
    type into a staged command. Holding one lock across the ladder keeps it
    atomic against every other composer writer (events/wake.py, wake_batch.py).
    """
    async with composer_action_lock(str(getattr(pane, "target", "") or "")):
        return await _send_terminal_compaction_command_locked(
            pane,
            command,
            session_id,
            cli_source=cli_source,
            mark_continuation_pending=mark_continuation_pending,
            clear_continuation_pending=clear_continuation_pending,
            schedule_continuation_readiness=schedule_continuation_readiness,
            continuation_readiness_capture_lines=continuation_readiness_capture_lines,
            observe_interrupt=observe_interrupt,
            turn_settled=turn_settled,
            settle_seconds=settle_seconds,
            interrupt_settle_seconds=interrupt_settle_seconds,
            rejection_settle_seconds=rejection_settle_seconds,
            composer_read=composer_read,
            on_command_submitting=on_command_submitting,
            seat_left=seat_left,
        )


async def _send_terminal_compaction_command_locked(
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
    on_command_submitting: Callable[[], None] | None = None,
    seat_left: Callable[[], bool] | None = None,
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
    command because its turn is still running (Grok) fails a compact command at
    once, since it may have started; ``/clear`` is interrupted again and resubmitted
    once before the delivery fails. The composer ledger gates
    the composer first: an exact pending command gets bare Enter without another
    text write. A human draft refuses the whole delivery with ``composer_occupied``
    before any key is sent, so the operator's draft and the live turn are both left
    alone; the agent retries once the draft is submitted. ``composer_read`` then
    reads the composer back after Enter, so a command the CLI typed but
    never submitted fails with ``command_not_submitted`` instead of reporting
    success on the strength of the write outcome. ``seat_left`` is checked
    immediately before every key and text write, including the first interrupt
    and each write after a wait: a CLI that left its pane fails the delivery with
    ``no_terminal_target`` before its shell or successor receives anything.
    """
    continuation_pending = False
    if seat_left is not None:
        pane = _SeatGuardedPane(pane, seat_left)
    try:
        writable, refuse_reason, composer_state = await composer_gate_for_write(
            pane, action=command, pending_payload=command
        )
        settle_wait_seconds, settle_poll_seconds = _turn_settle_wait_budget(settle_seconds)
        if not writable:
            logger.info(
                "Refusing %s for session %s: %s",
                command,
                session_id,
                refuse_reason,
            )
            error_code = (
                _COMPOSER_OCCUPIED_ERROR_CODE
                if composer_state == "draft"
                else _COMPOSER_UNKNOWN_ERROR_CODE
            )
            return (
                False,
                refuse_reason,
                False,
                {"error_code": error_code, "continuation_pending": False},
            )
        held_command = composer_state == "held"
        interrupt_key = _compact_interrupt_key(cli_source)
        interrupt_seconds = interrupt_settle_seconds if settle_seconds is None else settle_seconds
        rejection_seconds = rejection_settle_seconds if settle_seconds is None else settle_seconds
        confirm_seconds = (
            _COMPACTION_CONFIRM_SETTLE_SECONDS if settle_seconds is None else settle_seconds
        )
        verify_seconds = _SUBMIT_VERIFY_SETTLE_SECONDS if settle_seconds is None else settle_seconds
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
        submit_unverified = False
        # A compact command may have started despite a transient rejection view.
        # Never type it again; only the verified-submit ladder may retry Enter when
        # it can still see the original command in the composer.
        rejection_retries = (
            0 if command in _CLI_COMPACT_COMMANDS.values() else _COMPACTION_REJECTION_RETRIES
        )
        for resubmission in range(1 + rejection_retries):
            if resubmission:
                logger.warning(
                    "Session %s rejected %s while its task was still running; "
                    "interrupting again before resubmission %d of %d",
                    session_id,
                    command,
                    resubmission,
                    _COMPACTION_REJECTION_RETRIES,
                )
            # A rejection may race with a turn ending, so only the first submission waits.
            # The wait also returns once the armed turn has ended and goal mode has
            # already started the next one. That successor is still interrupted.
            if not resubmission and not held_command:
                await _wait_for_turn_to_settle(
                    turn_settled,
                    session_id,
                    command,
                    wait_seconds=settle_wait_seconds,
                    poll_seconds=settle_poll_seconds,
                )
            if not held_command and (
                turn_settled is None or (await asyncio.to_thread(turn_settled)) is not True
            ):
                interrupted, reason, detail = await _interrupt_turn(
                    pane,
                    interrupt_key,
                    session_id,
                    observe_interrupt,
                    settle_seconds=interrupt_seconds,
                    turn_settled=turn_settled,
                    presses=_CLI_INTERRUPT_PRESSES.get(cli_source or "", _INTERRUPT_ATTEMPTS),
                )
                if not interrupted:
                    if isinstance(pane, _SeatGuardedPane):
                        # A CLI that quit under its last press is gone, not unconfirmed.
                        await pane.require_seat()
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

            # The settle wait and interrupt ran since the first read, so that empty read
            # cannot authorize this write: read again, refuse a draft or unknown entry,
            # and drain only stale daemon text.
            writable, refuse_reason, composer_state = await composer_gate_for_write(
                pane, action=command, pending_payload=command
            )
            if not writable:
                if continuation_pending:
                    clear_continuation_pending()
                error_code = (
                    _COMPOSER_OCCUPIED_ERROR_CODE
                    if composer_state == "draft"
                    else _COMPOSER_UNKNOWN_ERROR_CODE
                )
                return (
                    False,
                    refuse_reason,
                    False,
                    {"error_code": error_code, "continuation_pending": False},
                )
            # Stale text of unknown length (after the interrupt) refuses only on a
            # positive draft frame after the drain; empty or unknown proceeds.
            drain = (
                DrainResult(True)
                if composer_state in {"empty", "held"}
                else await clear_composer(
                    pane, cli_source, composer_read, verify_seconds=verify_seconds
                )
            )
            if not drain.ok:
                if continuation_pending:
                    clear_continuation_pending()
                log_pane_failure(pane, session_id, "clearing the composer", drain.reason)
                return (
                    False,
                    f"composer could not be cleared before {command}: {drain.reason}",
                    False,
                    {"error_code": _COMPOSER_NOT_CLEAN_ERROR_CODE, "continuation_pending": False},
                )

            if on_command_submitting is not None:
                on_command_submitting()
            ok, reason, submit_detail = await _submit_command(
                pane,
                command,
                session_id,
                cli_source=cli_source,
                composer_read=composer_read,
                verify_seconds=verify_seconds,
            )
            if (
                not ok
                and submit_detail is not None
                and submit_detail.get("error_code") == ENTER_DELIVERY_UNCONFIRMED_ERROR_CODE
            ):
                # The text write included a newline, which may have launched /compact.
                # Keep the marker and await a provider boundary; never type it again.
                if schedule_continuation_readiness is not None:
                    schedule_continuation_readiness(readiness_before_command)
                return True, None, continuation_pending, {"enter_delivery_unconfirmed": True}
            submit_unverified = (
                not ok
                and submit_detail is not None
                and submit_detail.get("error_code") == SUBMIT_UNVERIFIED_ERROR_CODE
            )
            if submit_unverified:
                # Write and Enter were delivered but no read proved the command left the
                # composer. Never retype it: confirm a modal if one shows, watch for a
                # rejection, and leave the provider boundary as the only proof.
                ok = True
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
            # The CLI read the command to reject it, and no submit hook records that, so
            # the ledger would hold it and turn the resubmission into a bare Enter.
            record_composer_submit(pane.target)

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
        result_detail: dict[str, Any] = {}
        if not interrupt_sent:
            result_detail["interrupted"] = False
        if submit_unverified:
            result_detail["submit_unverified"] = True
        return True, None, continuation_pending, result_detail or None
    except _SeatLeftError:
        if continuation_pending:
            clear_continuation_pending()
        return (
            False,
            _SEAT_LEFT_REASON,
            False,
            {"error_code": NO_TERMINAL_TARGET_ERROR_CODE, "continuation_pending": False},
        )
