"""Helpers for resolving tmux server context from stored terminal metadata."""

from __future__ import annotations

import json
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


def parse_terminal_context_value(
    terminal_context: Mapping[str, Any] | str | None,
) -> dict[str, Any] | None:
    """Normalize stored terminal context from either JSON text or a mapping."""
    if not terminal_context:
        return None
    if isinstance(terminal_context, str):
        try:
            parsed = json.loads(terminal_context)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(terminal_context, Mapping):
        return dict(terminal_context)
    return None


def get_tmux_socket_path(terminal_context: Mapping[str, Any] | None) -> str | None:
    """Return the stored tmux socket path, if present."""
    if not terminal_context:
        return None
    socket_path = terminal_context.get("tmux_socket_path")
    if isinstance(socket_path, str) and socket_path:
        return socket_path
    return None


def get_tmux_socket_name(terminal_context: Mapping[str, Any] | None) -> str | None:
    """Return the stored tmux socket name, if present."""
    if not terminal_context:
        return None
    socket_name = terminal_context.get("tmux_socket_name")
    if isinstance(socket_name, str) and socket_name:
        return socket_name
    return None


def get_tmux_manager_for_context(
    terminal_context: Mapping[str, Any] | None,
    *,
    default_socket_name: str = "",
) -> TmuxSessionManager:
    """Build a tmux manager targeting the server recorded in terminal_context."""
    socket_path = get_tmux_socket_path(terminal_context)
    if socket_path:
        return TmuxSessionManager(TmuxConfig(socket_name="", socket_path=socket_path))
    socket_name = get_tmux_socket_name(terminal_context)
    if socket_name:
        return TmuxSessionManager(TmuxConfig(socket_name=socket_name))
    return TmuxSessionManager(TmuxConfig(socket_name=default_socket_name))


def get_tmux_prefix_for_context(
    terminal_context: Mapping[str, Any] | None,
    *,
    command: str = "tmux",
) -> list[str]:
    """Return the tmux CLI prefix for the recorded server context."""
    socket_path = get_tmux_socket_path(terminal_context)
    socket_name = get_tmux_socket_name(terminal_context)
    args = [command]
    if socket_path:
        args.extend(["-S", socket_path])
    elif socket_name:
        args.extend(["-L", socket_name])
    return args
