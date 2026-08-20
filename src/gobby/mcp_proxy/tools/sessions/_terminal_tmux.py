"""tmux target resolution and key delivery helpers for terminal tools."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.sessions.tmux_context import parse_terminal_context_value

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
_DEFAULT_COMPACT_INTERRUPT_KEY = "Escape"
_CLI_COMPACT_INTERRUPT_KEYS: dict[str, str] = {
    "codex": "C-c",
}
_DEFAULT_INTERRUPT_SETTLE_SECONDS = 0.1
_CODEX_INTERRUPT_SETTLE_SECONDS = 1.0
_CODEX_INTERRUPT_ATTEMPTS = 3
_CODEX_INTERRUPT_POLL_SECONDS = 0.05
_COMPACTION_REJECTION_SETTLE_SECONDS = 0.1
_COMPACTION_REJECTION_CAPTURE_LINES = 30
_COMPACTION_REJECTION_ERROR_CODE = "compaction_command_rejected"


def _compact_interrupt_key(source: str | None) -> str:
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
    tmux: TmuxSessionManager,
    target: str,
    *,
    lines: int = _COMPACTION_REJECTION_CAPTURE_LINES,
) -> str | None:
    try:
        output = await tmux.snapshot_lines(target, lines=lines)
    except (TimeoutError, OSError, RuntimeError):
        logger.debug("Failed to capture tmux target %s for compaction state check", target)
        return None
    if isinstance(output, str):
        return output
    return None


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


async def _send_tmux_keys(
    tmux: TmuxSessionManager,
    target: str,
    keys: str,
    session_id: str,
    *,
    literal: bool,
    action: str,
) -> tuple[bool, str | None]:
    """Send tmux keys and keep failures structured for MCP callers."""
    try:
        ok = await tmux.dispatch_keys(target, keys, literal=literal)
    except TimeoutError:
        logger.warning("Timed out %s to tmux target %s for %s", action, target, session_id)
        return False, f"tmux send-keys timed out for session {session_id} while {action}"
    except (OSError, RuntimeError) as exc:
        detail = str(exc) or type(exc).__name__
        logger.warning(
            "Failed %s to tmux target %s for %s: %s",
            action,
            target,
            session_id,
            detail,
            exc_info=True,
        )
        return False, f"tmux send-keys failed for session {session_id} while {action}: {detail}"

    if not ok:
        return False, f"tmux send-keys failed for session {session_id} while {action}"
    return True, None


async def _send_compaction_command(
    tmux: TmuxSessionManager,
    target: str,
    command: str,
    session_id: str,
) -> tuple[bool, str | None]:
    """Send a compaction command through tmux and keep failures structured."""
    return await _send_tmux_keys(
        tmux,
        target,
        f"{command}\n",
        session_id,
        literal=True,
        action="sending compaction command",
    )


async def _wait_for_codex_interrupt(
    observe_interrupt: Callable[[], bool | None],
    *,
    attempt_seconds: float,
    poll_seconds: float = _CODEX_INTERRUPT_POLL_SECONDS,
) -> bool | None:
    """Poll for a fresh Codex abort event during one interrupt attempt."""
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


async def _send_terminal_compaction_command(
    tmux: TmuxSessionManager,
    target: str,
    command: str,
    session_id: str,
    *,
    cli_source: str | None,
    mark_continuation_pending: Callable[[], bool],
    clear_continuation_pending: Callable[[], bool],
    schedule_continuation_readiness: Callable[[str | None], bool] | None = None,
    continuation_readiness_capture_lines: int | None = None,
    observe_codex_interrupt: Callable[[], bool | None] | None = None,
    settle_seconds: float | None = None,
    interrupt_settle_seconds: float = _DEFAULT_INTERRUPT_SETTLE_SECONDS,
    rejection_settle_seconds: float = _COMPACTION_REJECTION_SETTLE_SECONDS,
) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
    """Confirm interruption, persist continuation state, then compact."""
    continuation_pending = False
    if cli_source == "codex":
        if observe_codex_interrupt is None:
            return (
                False,
                "Codex rollout transcript is unavailable for interrupt confirmation",
                False,
                {
                    "error_code": "codex_interrupt_observation_unavailable",
                    "continuation_pending": False,
                },
            )
        continuation_pending = bool(mark_continuation_pending())
        if not continuation_pending:
            return (
                False,
                "failed to persist compact_self continuation before compaction",
                False,
                None,
            )

        attempt_seconds = interrupt_settle_seconds if settle_seconds is None else settle_seconds
        interrupted = False
        for _attempt in range(_CODEX_INTERRUPT_ATTEMPTS):
            ok, reason = await _send_tmux_keys(
                tmux,
                target,
                _compact_interrupt_key(cli_source),
                session_id,
                literal=False,
                action="sending compaction interrupt",
            )
            if not ok:
                clear_continuation_pending()
                return False, reason, False, None
            observed = await _wait_for_codex_interrupt(
                observe_codex_interrupt,
                attempt_seconds=attempt_seconds,
            )
            if observed is None:
                clear_continuation_pending()
                return (
                    False,
                    "Codex rollout transcript became unavailable during interrupt confirmation",
                    False,
                    {
                        "error_code": "codex_interrupt_observation_unavailable",
                        "continuation_pending": False,
                    },
                )
            if observed:
                interrupted = True
                break
        if not interrupted:
            clear_continuation_pending()
            return (
                False,
                "Codex did not confirm interruption after 3 attempts",
                False,
                {
                    "error_code": "codex_interrupt_unconfirmed",
                    "continuation_pending": False,
                },
            )
    else:
        ok, reason = await _send_tmux_keys(
            tmux,
            target,
            _compact_interrupt_key(cli_source),
            session_id,
            literal=False,
            action="sending compaction interrupt",
        )
        if not ok:
            return False, reason, False, None

        delay = interrupt_settle_seconds if settle_seconds is None else settle_seconds
        if delay > 0:
            await asyncio.sleep(delay)

    before_command = await _capture_pane_snapshot(tmux, target)
    readiness_before_command = before_command
    if (
        schedule_continuation_readiness is not None
        and continuation_readiness_capture_lines is not None
    ):
        readiness_before_command = await _capture_pane_snapshot(
            tmux,
            target,
            lines=continuation_readiness_capture_lines,
        )
    if cli_source != "codex":
        continuation_pending = bool(mark_continuation_pending())
    if schedule_continuation_readiness is not None:
        if not continuation_pending:
            return (
                False,
                "failed to persist compact_self continuation before compaction",
                False,
                None,
            )
    ok, reason = await _send_compaction_command(tmux, target, command, session_id)
    if not ok:
        if continuation_pending:
            clear_continuation_pending()
        return False, reason, False, None

    rejection_delay = rejection_settle_seconds if settle_seconds is None else settle_seconds
    if rejection_delay > 0:
        await asyncio.sleep(rejection_delay)

    rejection = _detect_compaction_rejection(
        before_command,
        await _capture_pane_snapshot(tmux, target),
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
            "Failed to schedule compact_self continuation readiness for session %s; "
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
