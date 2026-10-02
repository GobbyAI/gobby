"""Session-scope condition helpers bound to the rule engine's session manager."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.terminals.actor_scope import SESSION_ACTOR_PREFIX, ActorScopeError, resolve_actor_scope

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager


def send_keys_target_in_scope(
    session_manager: SessionManager | None, caller_ref: Any, target_ref: Any
) -> bool:
    """False only when a resolvable caller provably cannot reach the send_keys target.

    The caller and target resolve exactly as ``send_keys`` resolves them, and the
    decision is ``ActorScope.admits``: the caller's own session, its project, and
    its agent tree in either direction. A caller or target the tool refuses on its
    own (missing, unresolvable, autonomous agent) passes here so the tool keeps
    reporting that specific error. Any other failure raises, and a raising block
    condition fails closed.
    """
    if session_manager is None:
        raise RuntimeError("send_keys target scope needs a session manager")
    if not isinstance(caller_ref, str) or not caller_ref:
        return True
    if not isinstance(target_ref, str) or not target_ref:
        return True
    try:
        scope = resolve_actor_scope(session_manager, f"{SESSION_ACTOR_PREFIX}{caller_ref}")
    except ActorScopeError:
        return True
    caller = scope.caller
    if caller is None:
        return True
    try:
        target_id = session_manager.resolve_session_reference(target_ref, caller.project_id)
    except ValueError:
        return True
    target = session_manager.get(target_id)
    if target is None:
        return True
    return scope.admits(project_id=target.project_id, session_id=target_id)


def session_condition_helpers(
    session_manager: SessionManager | None,
) -> dict[str, Callable[..., Any]]:
    """Bind the session-scope helpers to the evaluator's session manager."""
    return {
        "send_keys_target_in_scope": lambda caller_ref, target_ref: send_keys_target_in_scope(
            session_manager, caller_ref, target_ref
        ),
    }
