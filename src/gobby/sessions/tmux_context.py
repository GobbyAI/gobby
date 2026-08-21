"""Helpers for resolving tmux server context from stored terminal metadata."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from typing import Any

from gobby import terminal_context as terminal_context_helpers
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig

merge_terminal_context = terminal_context_helpers.merge_terminal_context
parse_terminal_context_value = terminal_context_helpers.parse_terminal_context_value
terminal_context_has_tmux_target = terminal_context_helpers.terminal_context_has_tmux_target
_TMUX_IDENTITY_TIMEOUT_SECONDS = 0.5


def parse_tmux_socket_path(tmux_env: str | None) -> str | None:
    """Extract the tmux socket path from the TMUX env var."""
    if not isinstance(tmux_env, str):
        return None
    socket_path = tmux_env.split(",", 1)[0].strip()
    return socket_path or None


def query_tmux_identity(
    socket_path: str,
    pane_id: str,
    *,
    command: str = "tmux",
) -> tuple[str, str] | None:
    """Resolve stable window and session identity with a bounded tmux query."""
    if not socket_path or not pane_id.startswith("%") or not pane_id[1:].isdigit():
        return None
    try:
        result = subprocess.run(
            [
                command,
                "-S",
                socket_path,
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{window_id}\t#{session_name}",
            ],
            capture_output=True,
            text=True,
            timeout=_TMUX_IDENTITY_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    window_id, separator, session_name = result.stdout.strip().partition("\t")
    if separator and window_id.startswith("@") and window_id[1:].isdigit() and session_name:
        return window_id, session_name
    return None


def query_tmux_generation(
    socket_path: str,
    pane_id: str,
    *,
    command: str = "tmux",
) -> dict[str, object] | None:
    """Resolve pane generation (pid, start_time) plus window/session identity."""
    if not socket_path or not pane_id.startswith("%") or not pane_id[1:].isdigit():
        return None
    try:
        result = subprocess.run(
            [
                command,
                "-S",
                socket_path,
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{pid}\t#{start_time}\t#{window_id}\t#{session_name}",
            ],
            capture_output=True,
            text=True,
            timeout=_TMUX_IDENTITY_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split("\t")
    if len(parts) != 4:
        return None
    pid_raw, start_raw, window_id, session_name = parts
    if not (
        pid_raw.isdigit()
        and start_raw.isdigit()
        and window_id.startswith("@")
        and window_id[1:].isdigit()
        and session_name
    ):
        return None
    return {
        "server_pid": int(pid_raw),
        "server_start_time": int(start_raw),
        "window_id": window_id,
        "session_name": session_name,
        "pane_id": pane_id,
        "socket_path": socket_path,
    }


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


def get_tmux_session_name(terminal_context: Mapping[str, Any] | None) -> str | None:
    """Return the tmux session name stored in a terminal-context mapping."""
    if not terminal_context:
        return None
    value = terminal_context.get("tmux_session")
    return value if isinstance(value, str) and value else None


def get_tmux_window_id(terminal_context: Mapping[str, Any] | None) -> str | None:
    """Return the stable tmux window ID stored in a terminal-context mapping."""
    if not terminal_context:
        return None
    value = terminal_context.get("tmux_window_id")
    return (
        value if isinstance(value, str) and value.startswith("@") and value[1:].isdigit() else None
    )


def is_configured_tmux_socket(
    terminal_context: Mapping[str, Any] | None,
    *,
    config: TmuxConfig | None = None,
) -> bool | None:
    """Classify whether terminal context targets Gobby's configured spawn socket.

    ``None`` means the recorded identity is missing or conflicting, so callers
    should retain their conservative lifecycle behavior.
    """
    if config is None:
        try:
            from gobby.agents.tmux import get_configured_tmux_config

            config = get_configured_tmux_config()
        except RuntimeError:
            return None

    socket_path = get_tmux_socket_path(terminal_context)
    socket_name = get_tmux_socket_name(terminal_context)
    if not socket_path and not socket_name:
        return None

    if config.socket_path:
        if socket_path:
            return os.path.normpath(socket_path) == os.path.normpath(config.socket_path)
        return None

    configured_name = config.socket_name
    if not configured_name:
        return None

    matches: list[bool] = []
    if socket_name:
        matches.append(socket_name == configured_name)
    if socket_path:
        matches.append(os.path.basename(os.path.normpath(socket_path)) == configured_name)
    if not matches or any(match != matches[0] for match in matches[1:]):
        return None
    return matches[0]


def get_terminal_parent_pid(terminal_context: Mapping[str, Any] | None) -> int | None:
    """Return the positive parent-process PID stored in a terminal-context mapping."""
    if not terminal_context:
        return None
    value = terminal_context.get("parent_pid")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
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
