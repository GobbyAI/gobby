"""Pane resolution and command delivery for terminal handoff tools."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.sessions.tmux_context import parse_terminal_context_value
from gobby.terminals.pane_io import PaneIO, SendResult, clear_composer
from gobby.terminals.runtime import NamedKey

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

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
}
# Blind path (no transcript observer): wait this long after the interrupt key.
_DEFAULT_INTERRUPT_SETTLE_SECONDS = 0.1
# Observed path: poll the transcript this long per interrupt attempt.
_OBSERVED_INTERRUPT_SETTLE_SECONDS = 1.0
_INTERRUPT_ATTEMPTS = 3
_INTERRUPT_POLL_SECONDS = 0.05
_COMPACTION_REJECTION_SETTLE_SECONDS = 0.1
_COMPACTION_REJECTION_CAPTURE_LINES = 30
_COMPACTION_REJECTION_ERROR_CODE = "compaction_command_rejected"
_COMPOSER_NOT_CLEAN_ERROR_CODE = "composer_not_clean"
_INTERRUPT_UNCONFIRMED_ERROR_CODE = "interrupt_unconfirmed"
_INTERRUPT_OBSERVATION_UNAVAILABLE_ERROR_CODE = "interrupt_observation_unavailable"


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


async def _submit_command(pane: PaneIO, command: str, session_id: str) -> SendResult:
    """Type ``command`` into the drained composer and press Enter."""
    ok, reason = await pane.type_text(command)
    if not ok:
        _log_pane_failure(pane, session_id, "typing compaction command", reason)
        return False, reason
    return await _send_pane_key(pane, "enter", session_id, action="submitting compaction command")


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
    settle_seconds: float | None = None,
    interrupt_settle_seconds: float = _DEFAULT_INTERRUPT_SETTLE_SECONDS,
    rejection_settle_seconds: float = _COMPACTION_REJECTION_SETTLE_SECONDS,
) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
    """Confirm the interrupt, drain the composer, then submit the command.

    ``settle_seconds`` overrides every wait (tests); ``observe_interrupt`` is the
    transcript observer for CLIs that record interrupts, and its absence keeps the
    blind interrupt path for CLIs that do not.
    """
    continuation_pending = False
    interrupt_key = _compact_interrupt_key(cli_source)
    if observe_interrupt is not None:
        continuation_pending = bool(mark_continuation_pending())
        if not continuation_pending:
            return (
                False,
                "failed to persist handoff continuation before compaction",
                False,
                None,
            )
        attempt_seconds = interrupt_settle_seconds if settle_seconds is None else settle_seconds
        confirmed, reason, detail = await _confirm_interrupt(
            pane,
            interrupt_key,
            session_id,
            observe_interrupt,
            attempt_seconds=attempt_seconds,
        )
        if not confirmed:
            clear_continuation_pending()
            return False, reason, False, detail
    else:
        ok, reason = await _send_pane_key(
            pane, interrupt_key, session_id, action="sending compaction interrupt"
        )
        if not ok:
            return False, reason, False, None

        delay = interrupt_settle_seconds if settle_seconds is None else settle_seconds
        if delay > 0:
            await asyncio.sleep(delay)

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
    if observe_interrupt is None:
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

    ok, reason = await _submit_command(pane, command, session_id)
    if not ok:
        if continuation_pending:
            clear_continuation_pending()
        return False, reason, False, None

    rejection_delay = rejection_settle_seconds if settle_seconds is None else settle_seconds
    if rejection_delay > 0:
        await asyncio.sleep(rejection_delay)

    rejection = _detect_compaction_rejection(
        before_command,
        await _capture_pane_snapshot(pane),
        command,
    )
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
    return True, None, continuation_pending, None


def _resolve_tmux_target(
    session_id: str,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
    *,
    tmux_manager_factory: Callable[[dict[str, Any]], TmuxSessionManager],
) -> tuple[str | None, TmuxSessionManager | None, str | None]:
    """Resolve a session ID to a tmux target.

    Returns:
        (tmux_target, tmux_manager, error_message).
    """
    # Try agent run first (agent sessions have a terminals-row link)
    agent_run = agent_run_manager.get_by_session(session_id)
    if agent_run is not None:
        if agent_run.status not in ("running", "pending"):
            return None, None, f"Agent session is not running (status={agent_run.status})"
        if not agent_run.terminal_id:
            return None, None, "Agent session has no terminal (mode may be autonomous)"
        from gobby.agents.tmux.session_manager import TmuxSessionManager
        from gobby.storage.terminals import TerminalManager

        row = TerminalManager(agent_run_manager.db).get(agent_run.terminal_id)
        if row is None or not row.session_name:
            return None, None, "Agent session has no tmux target"
        return row.session_name, TmuxSessionManager(), None

    # Fallback: interactive CLI session with terminal_context
    session = session_manager.get(session_id)
    if session is None:
        return None, None, f"Session {session_id} not found"

    if session.terminal_context:
        ctx = parse_terminal_context_value(session.terminal_context)
        if ctx is None:
            raw_type = type(session.terminal_context).__name__
            return (
                None,
                None,
                f"Session {session_id} has invalid terminal_context ({raw_type}); "
                "expected object or JSON object",
            )
        # terminal_context may contain tmux_pane or tmux_session
        tmux_target = ctx.get("tmux_pane") or ctx.get("tmux_session")
        if tmux_target:
            return tmux_target, tmux_manager_factory(ctx), None
        keys = ", ".join(sorted(str(key) for key in ctx.keys())) or "none"
        return (
            None,
            None,
            f"Session {session_id} terminal_context has no tmux_pane or tmux_session "
            f"(keys: {keys})",
        )

    return None, None, f"Session {session_id} has no tmux terminal"
