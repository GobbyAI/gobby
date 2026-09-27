"""Provider-neutral terminal routing for wake and terminal input."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class LiveTerminalResolver(Protocol):
    def resolve_live_for_session(self, session: Any) -> Any | None: ...


@dataclass(frozen=True, slots=True)
class SessionTerminalRoute:
    managed_terminal: Any | None


def resolve_session_terminal_route(
    session: Any,
    terminal_manager: LiveTerminalResolver | None,
) -> SessionTerminalRoute:
    """Resolve the live terminal binding used for wake delivery."""
    managed = (
        None if terminal_manager is None else terminal_manager.resolve_live_for_session(session)
    )
    return SessionTerminalRoute(managed)
