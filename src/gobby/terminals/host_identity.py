"""PID-file and process-identity checks for gterm adoption."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from gobby.terminals.host_protocol import read_pidfile

logger = logging.getLogger(__name__)

PidIdentity = Callable[[int], bool]


def is_live_gterm(pid: int) -> bool:
    """Return whether ``pid`` is a live process whose image looks like gterm."""
    if pid <= 0:
        return False
    try:
        import psutil
    except ImportError:
        return _proc_exists(pid)
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        name = (process.name() or "").lower()
        cmdline = " ".join(process.cmdline()).lower()
        return "gterm" in name or "gterm" in cmdline
    except (psutil.Error, OSError):
        return False


def _proc_exists(pid: int) -> bool:
    try:
        Path(f"/proc/{pid}").stat()
        return True
    except OSError:
        try:
            import os

            os.kill(pid, 0)
            return True
        except OSError:
            return False


def pid_matches_ping(*, socket_dir: Path, host_pid: int, identity: PidIdentity) -> bool:
    """Adopt only when pidfile, ping.host_pid, and live gterm identity agree."""
    stored = read_pidfile(socket_dir)
    if stored is None or stored != host_pid:
        return False
    return identity(host_pid)
