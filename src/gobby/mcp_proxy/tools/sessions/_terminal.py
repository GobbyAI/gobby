"""Terminal interaction tools for tmux-backed sessions.

Exposes send_keys and capture_output as MCP tools on gobby-sessions,
enabling orchestration (conductor, heartbeat, pipelines, other agents)
to interact with running terminal sessions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.sessions.tmux_context import get_tmux_manager_for_context, parse_terminal_context_value
from gobby.storage.agents import LocalAgentRunManager

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


def _resolve_tmux_target(
    session_id: str,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
) -> tuple[str | None, TmuxSessionManager | None, str | None]:
    """Resolve a session ID to a tmux target.

    Returns:
        (tmux_target, tmux_manager, error_message).
    """
    # Try agent run first (agent sessions have tmux_session_name on the run)
    agent_run = agent_run_manager.get_by_session(session_id)
    if agent_run is not None:
        if agent_run.status not in ("running", "pending"):
            return None, None, f"Agent session is not running (status={agent_run.status})"
        if not agent_run.tmux_session_name:
            return None, None, "Agent session has no tmux terminal (mode may be autonomous)"
        return agent_run.tmux_session_name, TmuxSessionManager(TmuxConfig()), None

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
            return tmux_target, get_tmux_manager_for_context(ctx), None
        keys = ", ".join(sorted(str(key) for key in ctx.keys())) or "none"
        return (
            None,
            None,
            f"Session {session_id} terminal_context has no tmux_pane or tmux_session "
            f"(keys: {keys})",
        )

    return None, None, f"Session {session_id} has no tmux terminal"


def register_terminal_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
    db: DatabaseProtocol,
) -> None:
    """Register send_keys and capture_output tools."""

    agent_run_manager = LocalAgentRunManager(db)

    @registry.tool(
        name="send_keys",
        description=(
            "Send keystrokes to a session's tmux terminal. "
            "Use literal=true (default) to type text — trailing \\n sends Enter. "
            "Use literal=false for tmux key names: C-c, Escape, Enter, C-d."
        ),
    )
    async def send_keys(
        session_id: str,
        keys: str,
        literal: bool = True,
    ) -> dict[str, Any]:
        target, tmux, error = _resolve_tmux_target(session_id, session_manager, agent_run_manager)
        if error:
            return {"success": False, "error": error}

        assert target is not None
        assert tmux is not None
        ok = await tmux.send_keys(target, keys, literal=literal)
        if not ok:
            return {
                "success": False,
                "error": f"tmux send-keys failed for session {session_id}",
            }
        return {"success": True}

    @registry.tool(
        name="capture_output",
        description=(
            "Capture the last N lines of a session's tmux terminal output. "
            "Useful for inspecting permission dialogs, trust prompts, or "
            "other terminal state not visible through hooks."
        ),
    )
    async def capture_output(
        session_id: str,
        lines: int = 50,
    ) -> dict[str, Any]:
        target, tmux, error = _resolve_tmux_target(session_id, session_manager, agent_run_manager)
        if error:
            return {"success": False, "error": error}

        assert target is not None
        assert tmux is not None
        output = await tmux.capture_pane(target, lines)
        if output is None:
            return {
                "success": False,
                "error": f"Failed to capture pane for session {session_id}",
            }
        return {"success": True, "output": output}
