"""Provider-neutral terminal routing for wake and terminal input."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from gobby.terminal_context import parse_terminal_context_value


class LiveTerminalResolver(Protocol):
    def resolve_live_for_session(self, session: Any) -> Any | None: ...


@dataclass(frozen=True, slots=True)
class SessionTerminalRoute:
    managed_terminal: Any | None
    tmux_pane: str | None
    tmux_session: str | None
    tmux_socket_path: str | None
    has_terminal_context: bool


def resolve_session_terminal_route(
    session: Any,
    terminal_manager: LiveTerminalResolver | None,
) -> SessionTerminalRoute:
    """Resolve managed ownership first, retaining raw tmux as the fallback."""
    managed = (
        None if terminal_manager is None else terminal_manager.resolve_live_for_session(session)
    )
    context = parse_terminal_context_value(getattr(session, "terminal_context", None))
    if context is None:
        return SessionTerminalRoute(managed, None, None, None, False)

    pane = context.get("tmux_pane")
    tmux_session = context.get("tmux_session")
    socket_path = context.get("tmux_socket_path")
    return SessionTerminalRoute(
        managed_terminal=managed,
        tmux_pane=pane if isinstance(pane, str) and pane else None,
        tmux_session=tmux_session if isinstance(tmux_session, str) and tmux_session else None,
        tmux_socket_path=socket_path if isinstance(socket_path, str) and socket_path else None,
        has_terminal_context=True,
    )
