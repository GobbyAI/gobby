"""Shared daemon-tracked terminal termination."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from gobby.terminals.actor_scope import ActorScopeError, resolve_actor_scope

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager
    from gobby.storage.terminals import Terminal
    from gobby.terminals.runtime import TerminalRuntimeRegistry

TerminalTerminationErrorCode = Literal[
    "forbidden",
    "not_found",
    "not_live",
    "terminal_failed",
]


class TerminalTerminationError(RuntimeError):
    """A terminal termination outcome callers can serialize."""

    def __init__(self, code: TerminalTerminationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class TerminalStore(Protocol):
    """The terminal manager operations termination needs."""

    def get(self, terminal_id: str) -> Terminal | None: ...

    def get_live_for_session(self, session_id: str) -> Terminal | None: ...

    def mark_exited(self, terminal_id: str) -> Terminal | None: ...


def resolve_terminal_reference(
    terminals: TerminalStore,
    sessions: SessionManager,
    reference: str,
    *,
    project_id: str | None = None,
) -> Terminal | None:
    """Resolve a terminal id first, then a root-session reference."""
    try:
        terminal = terminals.get(reference)
    except (TypeError, ValueError):
        terminal = None
    if terminal is not None:
        return terminal

    try:
        session_id = sessions.resolve_session_reference(reference, project_id)
    except (TypeError, ValueError):
        return None
    return terminals.get_live_for_session(session_id)


async def kill_terminal(
    terminals: TerminalStore,
    registry: TerminalRuntimeRegistry,
    terminal: Terminal,
    *,
    grace_seconds: float = 1.0,
) -> Terminal | None:
    """Terminate one tracked row and synchronously mark it exited."""
    await registry.resolve(terminal.backend).terminate(terminal, grace_seconds)
    return terminals.mark_exited(terminal.id)


async def terminate_terminal(
    terminals: TerminalStore,
    registry: TerminalRuntimeRegistry,
    sessions: SessionManager,
    *,
    actor: str,
    reference: str,
) -> Terminal:
    """Authorize and terminate a live daemon-tracked terminal by either reference."""
    try:
        scope = resolve_actor_scope(sessions, actor)
    except ActorScopeError as exc:
        raise TerminalTerminationError("forbidden", str(exc)) from exc

    project_id = None if scope.caller is None else scope.caller.project_id
    terminal = resolve_terminal_reference(
        terminals,
        sessions,
        reference,
        project_id=project_id,
    )
    if terminal is None:
        raise TerminalTerminationError("not_found", f"Terminal or session {reference!r} not found")
    if not scope.admits(project_id=terminal.project_id, session_id=terminal.session_id):
        raise TerminalTerminationError(
            "forbidden",
            f"Terminal {terminal.id} is outside the actor's project and agent tree",
        )
    if terminal.state not in {"live", "orphaned"}:
        raise TerminalTerminationError("not_live", f"Terminal {terminal.id} is not live")

    try:
        exited = await kill_terminal(terminals, registry, terminal)
    except Exception as exc:
        raise TerminalTerminationError(
            "terminal_failed", f"Failed to terminate terminal {terminal.id}: {exc}"
        ) from exc
    if exited is None:
        raise TerminalTerminationError("not_live", f"Terminal {terminal.id} is not live")
    return exited
