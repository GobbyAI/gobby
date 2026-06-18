"""
Shared utilities for CLI commands.

This module is the stable compatibility surface for CLI helpers. Implementation
lives in focused modules, while tests and downstream callers can continue to
patch/import names from ``gobby.cli.utils``.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click
import psutil

from gobby.cli.utils_config import (
    _redact_dsn,
    get_gobby_home,
    get_install_dir,
    get_resources_dir,
    init_local_storage,
    load_full_config_from_db,
)
from gobby.cli.utils_process import (
    _is_process_alive,
    _kill_port_holder,
    format_uptime,
    is_port_available,
    kill_all_gobby_daemons,
    setup_logging,
    wait_for_port_available,
)
from gobby.cli.utils_resolution import (
    get_active_session_id,
    list_project_names,
    resolve_project_ref,
    resolve_session_id,
)
from gobby.cli.utils_shutdown import stop_daemon
from gobby.cli.utils_ui import (
    _open_ui_log_handler,
    _stop_step,
    find_web_dir,
    spawn_ui_server,
    stop_ui_server,
)
from gobby.config.app import DaemonConfig, load_config
from gobby.config.bootstrap import DEFAULT_WEBSOCKET_PORT
from gobby.config.ui import UIConfig
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)
_UI_LOG_MAX_BYTES = 5 * 1024 * 1024
_UI_LOG_BACKUP_COUNT = 3

__all__ = [
    "DEFAULT_WEBSOCKET_PORT",
    "DaemonConfig",
    "LocalProjectManager",
    "Path",
    "RotatingFileHandler",
    "SessionManager",
    "UIConfig",
    "_UI_LOG_BACKUP_COUNT",
    "_UI_LOG_MAX_BYTES",
    "_is_process_alive",
    "_kill_port_holder",
    "_open_ui_log_handler",
    "_redact_dsn",
    "_stop_step",
    "click",
    "find_web_dir",
    "format_uptime",
    "get_active_session_id",
    "get_gobby_home",
    "get_install_dir",
    "get_project_context",
    "get_resources_dir",
    "init_local_storage",
    "is_port_available",
    "kill_all_gobby_daemons",
    "list_project_names",
    "load_config",
    "load_full_config_from_db",
    "logger",
    "logging",
    "os",
    "psutil",
    "resolve_project_ref",
    "resolve_session_id",
    "setup_logging",
    "signal",
    "spawn_ui_server",
    "stop_daemon",
    "stop_ui_server",
    "time",
    "wait_for_port_available",
]
