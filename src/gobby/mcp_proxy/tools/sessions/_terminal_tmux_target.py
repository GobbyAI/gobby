"""Tmux target resolution for the sessions terminal tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.sessions.tmux_context import parse_terminal_context_value

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager


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
