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
from gobby.agents.tmux.pty_bridge import TmuxPTYBridge
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.agents.tmux.spawner import TmuxSpawner
from gobby.agents.tmux.wsl_compat import convert_windows_path_to_wsl, needs_wsl
from gobby.config.tmux import TmuxConfig

__all__ = [
    "TmuxConfig",
    "TmuxNotFoundError",
    "TmuxOutputReader",
    "TmuxPaneMonitor",
    "TmuxPTYBridge",
    "TmuxSessionError",
    "TmuxSessionManager",
    "TmuxSpawner",
    "convert_windows_path_to_wsl",
    "get_tmux_output_reader",
    "get_tmux_pane_monitor",
    "get_tmux_session_manager",
    "needs_wsl",
    "set_tmux_pane_monitor",
]

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------
_session_manager: TmuxSessionManager | None = None
_output_reader: TmuxOutputReader | None = None
_pane_monitor: TmuxPaneMonitor | None = None
_lock = threading.Lock()


def get_tmux_session_manager(config: TmuxConfig | None = None) -> TmuxSessionManager:
    """Return the global :class:`TmuxSessionManager` singleton."""
    global _session_manager
    if _session_manager is None:
        with _lock:
            if _session_manager is None:
                _session_manager = TmuxSessionManager(config)
    return _session_manager


def get_tmux_output_reader(config: TmuxConfig | None = None) -> TmuxOutputReader:
    """Return the global :class:`TmuxOutputReader` singleton."""
    global _output_reader
    if _output_reader is None:
        with _lock:
            if _output_reader is None:
                _output_reader = TmuxOutputReader(config)
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
