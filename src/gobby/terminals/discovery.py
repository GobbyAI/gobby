"""SessionStart and liveness discovery of external terminal rows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gobby.storage.terminals import (
    Terminal,
    TerminalManager,
    parse_tmux_generation,
    tmux_locator_key,
)


def generation_from_context(terminal_context: Mapping[str, Any]) -> dict[str, object] | None:
    """Build a tmux generation dict from stored terminal_context keys."""
    socket = terminal_context.get("tmux_socket_path")
    pane = terminal_context.get("tmux_pane")
    pid = terminal_context.get("tmux_server_pid")
    start = terminal_context.get("tmux_server_start_time")
    if not isinstance(socket, str) or not socket:
        return None
    if not isinstance(pane, str) or not pane:
        return None
    if not isinstance(pid, int) or not isinstance(start, int):
        return None
    return {
        "socket_path": socket,
        "server_pid": pid,
        "server_start_time": start,
        "pane_id": pane,
    }


def generation_from_display_message(raw: str) -> dict[str, object]:
    """Parse a batched display-message of socket|pid|start|pane."""
    return parse_tmux_generation(raw)


def seed_external_terminal(
    manager: TerminalManager,
    *,
    project_id: str,
    session_id: str | None,
    terminal_context: Mapping[str, Any],
    generation: Mapping[str, object] | None = None,
) -> Terminal | None:
    """Upsert an external row, or refresh a matching unpromoted managed pane."""
    resolved = (
        dict(generation) if generation is not None else generation_from_context(terminal_context)
    )
    if resolved is None:
        return None
    socket_path = str(resolved["socket_path"])
    raw_pid = resolved["server_pid"]
    raw_start = resolved["server_start_time"]
    if not isinstance(raw_pid, int) or not isinstance(raw_start, int):
        return None
    server_pid = raw_pid
    server_start_time = raw_start
    pane_id = str(resolved["pane_id"])
    locator_key = tmux_locator_key(
        socket_path=socket_path,
        server_pid=server_pid,
        server_start_time=server_start_time,
        pane_id=pane_id,
    )
    session_name = terminal_context.get("tmux_session")
    window_id = terminal_context.get("tmux_window")
    title = terminal_context.get("tmux_pane_title")
    if not isinstance(session_name, str):
        session_name = None
    if not isinstance(window_id, str):
        window_id = None
    if not isinstance(title, str):
        title = session_name
    if session_name:
        pending = manager.get_live_by_session_name(session_name)
        if pending is not None and pending.ownership == "gobby" and pending.state == "pending":
            return pending
    locator = {
        "socket_path": socket_path,
        "server_pid": server_pid,
        "server_start_time": server_start_time,
        "pane_id": pane_id,
    }
    return manager.upsert_external(
        project_id=project_id,
        backend="tmux",
        locator=locator,
        locator_key=locator_key,
        session_name=session_name,
        window_id=window_id,
        title=title,
        session_id=session_id,
    )
