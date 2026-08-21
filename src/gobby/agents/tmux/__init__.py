"""First-class tmux agent spawning module.

Provides session management, output streaming, and a promoted spawner
for tmux-based agent execution with web UI visibility.

Public API::

    from gobby.agents.tmux import (
        get_tmux_session_manager,
        get_tmux_output_reader,
        TmuxSpawner,
        TmuxConfig,
    )
"""

from __future__ import annotations

import threading

from gobby.agents.tmux.errors import TmuxNotFoundError, TmuxSessionError
from gobby.agents.tmux.output_reader import TmuxOutputReader
from gobby.agents.tmux.pane_monitor import TmuxPaneMonitor
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.agents.tmux.spawner import TmuxSpawner
from gobby.agents.tmux.wsl_compat import convert_windows_path_to_wsl, needs_wsl
from gobby.config.tmux import TmuxConfig

__all__ = [
    "TmuxConfig",
    "TmuxNotFoundError",
    "TmuxOutputReader",
    "TmuxPaneMonitor",
    "TmuxSessionError",
    "TmuxSessionManager",
    "TmuxSpawner",
    "convert_windows_path_to_wsl",
    "configure_tmux",
    "get_configured_tmux_config",
    "get_configured_tmux_command_prefix",
    "get_tmux_output_reader",
    "get_tmux_pane_monitor",
    "get_tmux_session_manager",
    "needs_wsl",
    "reset_tmux_globals",
    "reset_tmux_output_callback",
    "set_tmux_pane_monitor",
]

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------
_session_manager: TmuxSessionManager | None = None
_output_reader: TmuxOutputReader | None = None
_pane_monitor: TmuxPaneMonitor | None = None
_configured_tmux_config: TmuxConfig | None = None
_lock = threading.Lock()


def configure_tmux(config: TmuxConfig) -> None:
    """Configure daemon-wide tmux helpers from the daemon config."""
    global _configured_tmux_config, _output_reader, _session_manager
    with _lock:
        if _configured_tmux_config == config and _session_manager and _output_reader:
            return
        _configured_tmux_config = config
        _session_manager = TmuxSessionManager(config)
        _output_reader = TmuxOutputReader(config)


def get_configured_tmux_config() -> TmuxConfig:
    """Return the daemon tmux config after startup initialization."""
    with _lock:
        if _configured_tmux_config is None:
            raise RuntimeError("tmux helpers have not been configured from daemon config")
        return _configured_tmux_config


def get_tmux_session_manager_for(
    *,
    socket_name: str | None = None,
    socket_path: str | None = None,
) -> TmuxSessionManager:
    """Return the daemon session manager, or a socket-overridden instance."""
    if socket_name is None and socket_path is None:
        return get_tmux_session_manager()
    config = get_configured_tmux_config().model_copy(
        update={
            "socket_name": get_configured_tmux_config().socket_name
            if socket_name is None
            else socket_name,
            "socket_path": socket_path,
        }
    )
    return TmuxSessionManager(config)


def tmux_command_prefix(config: TmuxConfig) -> list[str]:
    """Build the tmux command prefix for a config, including socket selection."""
    command = [config.command]
    if config.socket_path:
        command.extend(["-S", config.socket_path])
    elif config.socket_name:
        command.extend(["-L", config.socket_name])
    return command


def get_configured_tmux_command_prefix() -> list[str]:
    """Build a tmux command prefix from the daemon tmux config."""
    return tmux_command_prefix(get_configured_tmux_config())


def get_tmux_session_manager(config: TmuxConfig | None = None) -> TmuxSessionManager:
    """Return the global :class:`TmuxSessionManager` singleton."""
    if config is not None:
        configure_tmux(config)
    with _lock:
        if _session_manager is None:
            raise RuntimeError("tmux session manager requested before daemon tmux configuration")
        return _session_manager


def get_tmux_output_reader(config: TmuxConfig | None = None) -> TmuxOutputReader:
    """Return the global :class:`TmuxOutputReader` singleton."""
    if config is not None:
        configure_tmux(config)
    with _lock:
        if _output_reader is None:
            raise RuntimeError("tmux output reader requested before daemon tmux configuration")
        return _output_reader


def get_tmux_pane_monitor() -> TmuxPaneMonitor | None:
    """Return the global :class:`TmuxPaneMonitor`, or ``None`` if not started."""
    with _lock:
        return _pane_monitor


def set_tmux_pane_monitor(monitor: TmuxPaneMonitor | None) -> None:
    """Set (or clear) the global :class:`TmuxPaneMonitor` singleton."""
    global _pane_monitor
    with _lock:
        _pane_monitor = monitor


def reset_tmux_globals() -> None:
    """Clear daemon-owned tmux singletons after failed construction."""
    global _configured_tmux_config, _output_reader, _pane_monitor, _session_manager
    with _lock:
        _configured_tmux_config = None
        _session_manager = None
        _output_reader = None
        _pane_monitor = None


def reset_tmux_output_callback() -> None:
    """Clear the output callback without constructing daemon tmux state."""
    with _lock:
        if _output_reader is not None:
            _output_reader.set_output_callback(None)
