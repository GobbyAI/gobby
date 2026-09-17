"""Caller and target scope for effects on terminals (plan gclient-workspaces 2.1).

An actor is ``operator`` or ``session:<id>``. The operator is in scope
everywhere. A session actor must be an interactive session (an autonomous
agent-run session is refused) and reaches a target in its own project, the
session itself, or a session in its agent tree. ``send_keys`` and every
workspace op that kills, spawns into, adopts, or writes a terminal share it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager

OPERATOR_ACTOR = "operator"
SESSION_ACTOR_PREFIX = "session:"

ActorScopeRefusal = Literal[
    "invalid_actor", "caller_unresolved", "caller_not_found", "autonomous_agent"
]


class ActorScopeError(Exception):
    """The actor cannot act on terminals; ``reason`` says why."""

    def __init__(
        self,
        reason: ActorScopeRefusal,
        detail: str,
        *,
        caller_session_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason: ActorScopeRefusal = reason
        self.detail = detail
        self.caller_session_id = caller_session_id


@dataclass(frozen=True, slots=True)
class ActorScope:
    """A resolved actor; ``caller`` is None for the operator."""

    sessions: SessionManager
    caller: Session | None = None

    def admits(self, *, project_id: str | None, session_id: str | None = None) -> bool:
        """Whether a target in ``project_id``, bound to ``session_id``, is in scope."""
        caller = self.caller
        if caller is None or project_id == caller.project_id:
            return True
        if session_id is None:
            return False
        return (
            session_id == caller.id
            or self.sessions.is_ancestor(caller.id, session_id)
            or self.sessions.is_ancestor(session_id, caller.id)
        )


def resolve_actor_scope(session_manager: SessionManager, actor: str) -> ActorScope:
    """Resolve ``operator`` or ``session:<ref>``; raise ActorScopeError when refused."""
    if actor == OPERATOR_ACTOR:
        return ActorScope(session_manager)
    reference = actor.removeprefix(SESSION_ACTOR_PREFIX)
    if reference == actor or not reference:
        raise ActorScopeError(
            "invalid_actor", f"Actor must be 'operator' or 'session:<id>', got {actor!r}"
        )
    try:
        caller_id = session_manager.resolve_session_reference(reference)
    except ValueError as exc:
        raise ActorScopeError("caller_unresolved", str(exc)) from exc
    caller = session_manager.get(caller_id)
    if caller is None:
        raise ActorScopeError(
            "caller_not_found",
            f"Caller session {caller_id} not found",
            caller_session_id=caller_id,
        )
    if caller.agent_run_id:
        raise ActorScopeError(
            "autonomous_agent",
            "Autonomous agent sessions cannot act on terminals",
            caller_session_id=caller_id,
        )
    return ActorScope(session_manager, caller)
