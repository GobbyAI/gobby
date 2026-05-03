"""Terminal interaction tools for tmux-backed sessions.

Exposes send_keys, capture_output, and compact_self as MCP tools on
gobby-sessions, enabling orchestration (conductor, heartbeat, pipelines,
other agents) to interact with running terminal sessions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.sessions.compact_continuation import (
    clear_compact_self_continuation_pending,
    mark_compact_self_continuation_pending,
)
from gobby.sessions.tmux_context import get_tmux_manager_for_context, parse_terminal_context_value
from gobby.storage.agents import LocalAgentRunManager

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

# Maps session.source (CLI provider name) to the slash command that triggers
# context compaction in the running CLI subprocess.
_CLI_COMPACT_COMMANDS: dict[str, str] = {
    "claude": "/compact",
    "codex": "/compact",
    "gemini": "/compress",
    "qwen": "/compress",
    "droid": "/compress",
}
_CODEX_INTERRUPT_KEY = "Escape"
_CODEX_INTERRUPT_SETTLE_SECONDS = 0.2


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
        ok = await tmux.send_keys(target, keys, literal=literal)
    except TimeoutError:
        logger.warning("Timed out %s to tmux target %s for %s", action, target, session_id)
        return False, f"tmux send-keys timed out for session {session_id} while {action}"
    except Exception as exc:
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


async def _send_codex_compaction_command(
    tmux: TmuxSessionManager,
    target: str,
    command: str,
    session_id: str,
    *,
    settle_seconds: float | None = None,
) -> tuple[bool, str | None]:
    """Backward-compatible wrapper for tests around the terminal compaction flow."""
    ok, reason, _continuation_pending = await _send_terminal_compaction_command(
        tmux,
        target,
        command,
        session_id,
        mark_continuation_pending=lambda: False,
        clear_continuation_pending=lambda: False,
        settle_seconds=settle_seconds,
    )
    return ok, reason


async def _send_terminal_compaction_command(
    tmux: TmuxSessionManager,
    target: str,
    command: str,
    session_id: str,
    *,
    mark_continuation_pending: Any,
    clear_continuation_pending: Any,
    settle_seconds: float | None = None,
) -> tuple[bool, str | None, bool]:
    """Interrupt the active prompt, mark continuation pending, then compact."""
    ok, reason = await _send_tmux_keys(
        tmux,
        target,
        _CODEX_INTERRUPT_KEY,
        session_id,
        literal=False,
        action="sending compaction interrupt",
    )
    if not ok:
        return False, reason, False

    delay = _CODEX_INTERRUPT_SETTLE_SECONDS if settle_seconds is None else settle_seconds
    if delay > 0:
        await asyncio.sleep(delay)

    continuation_pending = bool(mark_continuation_pending())
    ok, reason = await _send_compaction_command(tmux, target, command, session_id)
    if not ok:
        if continuation_pending:
            clear_continuation_pending()
        return False, reason, False
    return True, None, continuation_pending


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


def _resolve_session_for_compaction(
    session_id: str,
    session_manager: SessionManager,
) -> tuple[str | None, Any | None, str | None]:
    """Resolve a user-facing session ref for compact_self."""
    resolved_id = session_id
    resolver = getattr(session_manager, "resolve_session_reference", None)
    if callable(resolver):
        try:
            from gobby.utils.project_context import get_project_context

            project_ctx = get_project_context()
            project_id = project_ctx.get("id") if project_ctx else None
            candidate = resolver(session_id, project_id)
            if isinstance(candidate, str) and candidate:
                resolved_id = candidate
        except ValueError as exc:
            return None, None, f"Session {session_id} not found: {exc}"
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            logger.warning(
                "Failed resolving session reference %r for compaction: %s",
                session_id,
                detail,
                exc_info=True,
            )
            return None, None, f"failed to resolve session {session_id}: {detail}"

    session = session_manager.get(resolved_id)
    if session is None:
        return resolved_id, None, f"Session {session_id} not found"
    return resolved_id, session, None


def register_terminal_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
    db: DatabaseProtocol,
    web_chat_session_registry: WebChatSessionRegistry | None = None,
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
        name="compact_self",
        description=(
            "Trigger context compaction in the calling session's CLI by firing "
            "the appropriate slash command (/compact for Claude Code, "
            "/compact for Codex, /compress for other supported CLIs). Designed "
            "to be called at workflow handoff boundaries — e.g. /gobby plan calls this after spawning "
            "plan-adversary so the coordinator's bulky requirements-gathering "
            "context is summarized away while the sub-agent runs. Web-chat "
            "sessions use the live daemon ChatSession registry."
        ),
    )
    async def compact_self(session_id: str) -> dict[str, Any]:
        resolved_session_id, session, error = _resolve_session_for_compaction(
            session_id,
            session_manager,
        )
        if error:
            return {"compacted": False, "reason": error}
        assert resolved_session_id is not None
        assert session is not None

        session_type = getattr(session, "session_type", "terminal")
        source = getattr(session, "source", None)

        if session_type == "web_chat":
            if web_chat_session_registry is None:
                return {
                    "compacted": False,
                    "reason": "web_chat session registry is not available",
                }
            if (
                resolved_session_id != session_id
                and web_chat_session_registry.find_session(resolved_session_id)[1] is None
            ):
                return await web_chat_session_registry.compact_session(session_id)
            return await web_chat_session_registry.compact_session(resolved_session_id)

        if session_type != "terminal":
            return {
                "compacted": False,
                "reason": f"unsupported session_type: {session_type}",
            }

        command = _CLI_COMPACT_COMMANDS.get(source) if source else None
        if command is None:
            return {
                "compacted": False,
                "reason": f"no compaction command known for cli={source!r}",
            }

        target, tmux, error = _resolve_tmux_target(
            resolved_session_id,
            session_manager,
            agent_run_manager,
        )
        if error:
            return {"compacted": False, "reason": error}
        assert target is not None
        assert tmux is not None

        ok, reason, continuation_pending = await _send_terminal_compaction_command(
            tmux,
            target,
            command,
            resolved_session_id,
            mark_continuation_pending=lambda: mark_compact_self_continuation_pending(
                db,
                resolved_session_id,
            ),
            clear_continuation_pending=lambda: clear_compact_self_continuation_pending(
                db,
                resolved_session_id,
            ),
        )

        if not ok:
            return {
                "compacted": False,
                "reason": reason,
            }
        result = {
            "compacted": True,
            "command": command,
            "cli": source,
            "via": "tmux",
            "interrupted": True,
            "continuation_pending": continuation_pending,
        }
        return result

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
