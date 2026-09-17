"""Authorization, idempotency, and delivery for the sessions ``send_keys`` tool."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.storage.agents import LocalAgentRunManager
from gobby.terminals.actor_scope import SESSION_ACTOR_PREFIX, ActorScopeError, resolve_actor_scope
from gobby.terminals.lookup import manager_for_terminal_context
from gobby.terminals.runtime import Delivered, IndeterminateWrite, is_named_key
from gobby.terminals.write_coordinator import IdempotencyConflictError, WriteRequest

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

_FORBIDDEN_SPEED_COMMANDS = frozenset({"/fast"})
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")

__all__ = [
    "_FORBIDDEN_SPEED_COMMANDS",
    "_authorize_send_keys_target",
    "_is_speed_command",
    "_resolve_tmux_target",
    "register_send_keys_tool",
]


def _resolve_tmux_target(
    session_id: str,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
) -> tuple[str | None, TmuxSessionManager | None, str | None]:
    """Resolve a session ID to a tmux target through this module's patchable facade."""
    from gobby.mcp_proxy.tools.sessions._terminal_tmux import _resolve_tmux_target as resolve

    return resolve(
        session_id,
        session_manager,
        agent_run_manager,
        tmux_manager_factory=manager_for_terminal_context,
    )


def _is_speed_command(keys: str) -> bool:
    r"""Report whether a send_keys payload toggles provider speed mode.

    `/fast` is Claude Code's in-session speed switch and the only such toggle
    across the six supported CLIs; the constant is the seam for any that appear.
    Matching is on the first whitespace-separated token, casefolded, so `/fast\n`
    and `  /FAST  ` are caught while `/faster` passes through.
    """
    tokens = keys.split()
    return bool(tokens) and tokens[0].casefold() in _FORBIDDEN_SPEED_COMMANDS


def _authorize_send_keys_target(
    session_ref: str,
    session_manager: SessionManager,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a send_keys target and verify it is within the caller's scope."""
    from gobby.utils.session_context import get_current_session_id

    caller_ref = get_current_session_id()
    if not caller_ref:
        return None, {
            "success": False,
            "error": "send_keys requires current MCP SessionContext",
            "error_code": "send_keys_caller_required",
        }

    try:
        scope = resolve_actor_scope(session_manager, f"{SESSION_ACTOR_PREFIX}{caller_ref}")
    except ActorScopeError as exc:
        if exc.reason == "autonomous_agent":
            return None, {
                "success": False,
                "error": "Autonomous agent sessions cannot use send_keys",
                "error_code": "send_keys_autonomous_agent_forbidden",
                "caller_session_id": exc.caller_session_id,
            }
        error = (
            f"Send_keys caller session {exc.caller_session_id} not found"
            if exc.reason == "caller_not_found"
            else f"Could not resolve send_keys caller: {exc.detail}"
        )
        return None, {
            "success": False,
            "error": error,
            "error_code": "send_keys_caller_not_found",
        }
    caller = scope.caller
    if caller is None:
        return None, {
            "success": False,
            "error": f"Send_keys caller session {caller_ref} not found",
            "error_code": "send_keys_caller_not_found",
        }

    try:
        target_id = session_manager.resolve_session_reference(session_ref, caller.project_id)
    except ValueError as exc:
        return None, {
            "success": False,
            "error": str(exc),
            "error_code": "send_keys_target_not_found",
        }

    target = session_manager.get(target_id)
    if target is None:
        return None, {
            "success": False,
            "error": f"Session {session_ref} not found",
            "error_code": "send_keys_target_not_found",
        }

    if scope.admits(project_id=target.project_id, session_id=target_id):
        return target_id, None

    return None, {
        "success": False,
        "error": "send_keys target is outside the caller's project and agent tree",
        "error_code": "send_keys_target_forbidden",
        "caller_session_id": caller.id,
        "target_session_id": target_id,
    }


def register_send_keys_tool(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
    db: HubDatabase,
    *,
    terminal_manager: Any | None = None,
    write_coordinator: Any | None = None,
) -> None:
    """Register coordinator-backed terminal input with the sessions tool registry."""
    agent_run_manager = LocalAgentRunManager(db)

    @registry.tool(
        name="send_keys",
        description=(
            "Send keystrokes to a session's terminal. This is for terminal control; use "
            "`gobby-agents:send_message` for direct cross-session agent communication. "
            "Autonomous agent-run sessions cannot use this tool. Targets must be the caller, "
            "in the same project, or in the same agent tree. Use literal=true (default) to "
            "paste text — one or more trailing \\n characters produce exactly one Enter after "
            "the literal paste settles. Use literal=false for tmux key names. An optional "
            "idempotency_key is 1 to 128 characters from "
            "[A-Za-z0-9._:-]; retries preserve indeterminate outcomes without redispatch."
        ),
    )
    async def send_keys(
        session_id: str,
        keys: str,
        literal: bool = True,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if (
            idempotency_key is not None
            and _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None
        ):
            return {
                "success": False,
                "error": "idempotency_key must be 1 to 128 characters from [A-Za-z0-9._:-]",
                "error_code": "invalid_idempotency_key",
            }
        resolved_key = idempotency_key or uuid4().hex
        resolved_session_id, authorization_error = _authorize_send_keys_target(
            session_id,
            session_manager,
        )
        if authorization_error is not None:
            return {**authorization_error, "idempotency_key": resolved_key}
        if _is_speed_command(keys):
            return {
                "success": False,
                "error": "send_keys cannot toggle provider speed mode; ask the user to run it",
                "error_code": "send_keys_speed_command_forbidden",
                "idempotency_key": resolved_key,
            }

        assert resolved_session_id is not None
        if write_coordinator is not None and terminal_manager is not None:
            terminal = terminal_manager.get_live_for_session(resolved_session_id)
            if terminal is not None:
                kind: Literal["text", "key", "paste"] = "text"
                payload = keys
                submit = False
                if literal and keys.endswith("\n"):
                    payload = keys.rstrip("\n")
                    submit = True
                elif not literal and is_named_key(keys.lower()):
                    kind = "key"
                    payload = keys.lower()
                try:
                    outcome = await write_coordinator.write(
                        WriteRequest(
                            terminal_id=terminal.id,
                            action_key=f"mcp-send-keys:{resolved_session_id}:{resolved_key}",
                            origin="daemon",
                            kind=kind,
                            payload=payload,
                            submit=submit,
                            idempotency_key=resolved_key,
                        )
                    )
                except IdempotencyConflictError as exc:
                    return {
                        "success": False,
                        "error": str(exc),
                        "error_code": exc.code,
                        "idempotency_key": resolved_key,
                    }
                if isinstance(outcome, IndeterminateWrite):
                    return {
                        "success": False,
                        "indeterminate": True,
                        "error": outcome.detail or "send_keys write was indeterminate",
                        "idempotency_key": resolved_key,
                    }
                if not isinstance(outcome, Delivered):
                    return {
                        "success": False,
                        "error": "send_keys failed",
                        "idempotency_key": resolved_key,
                    }
                return {"success": True, "idempotency_key": resolved_key}

        target, tmux, error = _resolve_tmux_target(
            resolved_session_id,
            session_manager,
            agent_run_manager,
        )
        if error:
            return {"success": False, "error": error, "idempotency_key": resolved_key}
        assert target is not None
        assert tmux is not None
        if not await tmux.dispatch_keys(target, keys, literal=literal):
            return {
                "success": False,
                "error": f"tmux send-keys failed for session {session_id}",
                "idempotency_key": resolved_key,
            }
        return {"success": True, "idempotency_key": resolved_key}
