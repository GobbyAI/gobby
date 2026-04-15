"""Helpers for resolving tmux server context from stored terminal metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig


def parse_tmux_socket_path(tmux_env: str | None) -> str | None:
    """Extract the tmux socket path from the TMUX env var."""
    if not isinstance(tmux_env, str):
        return None
    socket_path = tmux_env.split(",", 1)[0].strip()
    return socket_path or None


def get_tmux_socket_path(terminal_context: Mapping[str, Any] | None) -> str | None:
    """Return the stored tmux socket path, if present."""
    if not terminal_context:
        return None
    socket_path = terminal_context.get("tmux_socket_path")
    if isinstance(socket_path, str) and socket_path:
        return socket_path
    return None


def get_tmux_manager_for_context(
    terminal_context: Mapping[str, Any] | None,
) -> TmuxSessionManager:
    """Build a tmux manager targeting the server recorded in terminal_context."""
    socket_path = get_tmux_socket_path(terminal_context)
    if socket_path:
        return TmuxSessionManager(TmuxConfig(socket_name="", socket_path=socket_path))
    return TmuxSessionManager(TmuxConfig(socket_name=""))


def get_tmux_prefix_for_context(
    terminal_context: Mapping[str, Any] | None,
    *,
    command: str = "tmux",
) -> list[str]:
    """Return the tmux CLI prefix for the recorded server context."""
    socket_path = get_tmux_socket_path(terminal_context)
    args = [command]
    if socket_path:
        args.extend(["-S", socket_path])
    return args
