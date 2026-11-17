"""Resolve whether terminal automation has a live managed target."""

from typing import Any


def detect_native_terminal_hosting(
    handler: Any, session_id: str, variables: dict[str, Any]
) -> None:
    """Refresh hosting once at session start and each turn boundary."""
    variables.pop("native_terminal_hosted", None)
    terminal_manager = getattr(handler, "terminal_manager", None)
    session_manager = getattr(handler, "_session_manager", None)
    if terminal_manager is None or session_manager is None:
        return
    session = session_manager.get(session_id)
    if session is None or session.session_type != "terminal":
        return
    variables["native_terminal_hosted"] = (
        terminal_manager.resolve_live_for_session(session) is not None
    )
