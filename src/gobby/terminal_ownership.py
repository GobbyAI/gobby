"""Canonical terminal-session identity and ownership ordering."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

TerminalIdentity = tuple[str, str, str]

_TMUX_SOCKET_FIELDS = (
    "tmux_socket_path",
    "tmux_socket_name",
    "tmux_socket",
)


def _non_empty_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def terminal_session_identity(session: object) -> TerminalIdentity | None:
    """Return machine/socket/pane identity when stored metadata is unambiguous."""
    terminal_context = getattr(session, "terminal_context", None)
    if not isinstance(terminal_context, Mapping):
        return None

    pane = _non_empty_text(terminal_context.get("tmux_pane"))
    if pane is None:
        return None

    socket_identity = None
    for field_name in _TMUX_SOCKET_FIELDS:
        socket_value = _non_empty_text(terminal_context.get(field_name))
        if socket_value is not None:
            socket_identity = f"{field_name}:{socket_value}"
            break
    if socket_identity is None:
        return None

    machine_id = _non_empty_text(getattr(session, "machine_id", None)) or ""
    return machine_id, socket_identity, pane


def terminal_session_creation_order(session: object) -> tuple[float, str]:
    """Return a deterministic immutable ordering key for terminal ownership."""
    created_at = getattr(session, "created_at", None)
    created_timestamp = (
        created_at.timestamp() if isinstance(created_at, datetime) else float("-inf")
    )

    session_id = _non_empty_text(getattr(session, "id", None)) or ""
    return created_timestamp, session_id
