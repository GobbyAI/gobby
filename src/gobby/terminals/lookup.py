"""Resolve live-or-pending terminal rows from agent runs and sessions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from gobby.storage.terminals import Terminal

ACTIVE_TERMINAL_STATES = frozenset({"pending", "live"})


class _HasGet(Protocol):
    def get(self, terminal_id: str) -> Terminal | None: ...


def active_terminal_for_run(manager: _HasGet | None, run: Any) -> Terminal | None:
    """Return the run's terminal when it is still pending or live."""
    if manager is None:
        return None
    terminal_id = getattr(run, "terminal_id", None)
    if not isinstance(terminal_id, str) or not terminal_id:
        return None
    row = manager.get(terminal_id)
    if row is None or row.state not in ACTIVE_TERMINAL_STATES:
        return None
    return row


def attach_name_from_context(terminal_context: Mapping[str, object] | None) -> str | None:
    """Session name from terminal_context without owned-module tmux field reads."""
    from gobby.sessions.tmux_context import get_tmux_session_name as session_name_of

    return session_name_of(terminal_context)


def socket_path_from_context(terminal_context: Mapping[str, object] | None) -> str | None:
    from gobby.sessions.tmux_context import get_tmux_socket_path as socket_path_of

    return socket_path_of(terminal_context)


def manager_for_terminal_context(terminal_context: Mapping[str, Any] | None) -> Any:
    from gobby.sessions.tmux_context import get_tmux_manager_for_context as manager_of

    return manager_of(terminal_context)


def parent_pid_from_context(terminal_context: Mapping[str, object] | None) -> int | None:
    from gobby.sessions.tmux_context import get_terminal_parent_pid as parent_pid_of

    return parent_pid_of(terminal_context)


def active_terminal_by_id(manager: _HasGet | None, terminal_id: str | None) -> Terminal | None:
    if manager is None or not terminal_id:
        return None
    row = manager.get(terminal_id)
    if row is None or row.state not in ACTIVE_TERMINAL_STATES:
        return None
    return row
