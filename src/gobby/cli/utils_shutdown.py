"""Daemon shutdown helper for CLI utilities."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any, cast

import click
import psutil

from gobby.cli.utils_runtime import facade
from gobby.utils.env import is_test_protect_enabled


def _report_lock_survivor(deps: Any, quiet: bool) -> None:
    """After a stop, surface any process still holding the daemon lock.

    A held lock at this point means an orphaned daemon the kill sequence
    missed (e.g. reparented past the pid file's record).
    """
    from gobby.runner_pid_file import ProbeState, probe_daemon_lock

    pid_file = cast(Path, deps.get_gobby_home() / "gobby.pid")
    owner = probe_daemon_lock(pid_file)
    if owner.state is ProbeState.ABSENT:
        return
    pid = owner.pid or "unknown"
    deps.logger.warning("Daemon lock still held by PID %s after stop", pid)
    if not quiet:
        deps._stop_step(
            f"Warning: daemon lock still held by PID {pid} — "
            "an orphaned daemon may still be running",
            error=True,
        )


def stop_daemon(
    quiet: bool = False,
    *,
    shutdown_intent: str = "stop",
    shutdown_source: str = "cli_stop",
) -> bool:
    """Stop the daemon process. Returns True on success, False on failure."""
    deps = facade()

    if is_test_protect_enabled():
        deps.logger.warning("stop_daemon called during test - skipping")
        return True

    if not quiet:
        click.echo("Stopping Gobby daemon...")

    deps.stop_ui_server(quiet=True)

    pid_file = cast(Path, deps.get_gobby_home() / "gobby.pid")

    pid: int | None = None
    if pid_file.exists():
        try:
            with open(pid_file) as file:
                pid = int(file.read().strip())
        except (OSError, ValueError) as exc:
            if not quiet:
                deps._stop_step(f"Error reading PID file: {exc}", error=True)
            pid_file.unlink(missing_ok=True)

    if pid is None:
        from gobby.cli.installers.service import get_service_status

        svc = get_service_status()
        if svc.get("running") and svc.get("pid"):
            pid = int(svc["pid"])
        else:
            if not quiet:
                deps._stop_step("Daemon is not running")
            return True

    if not bool(deps._is_process_alive(pid)):
        pid_file.unlink(missing_ok=True)
        killed = int(deps.kill_all_gobby_daemons())
        if not quiet:
            if killed > 0:
                deps._stop_step(f"Cleaned up {killed} orphaned process(es)")
            else:
                deps._stop_step("Daemon is not running (stale PID file removed)")
        return True

    try:
        proc = psutil.Process(pid)
        cmdline_str = " ".join(proc.cmdline())
        if "gobby" not in cmdline_str.lower():
            pid_file.unlink(missing_ok=True)
            killed = int(deps.kill_all_gobby_daemons())
            if not quiet:
                if killed > 0:
                    deps._stop_step(f"Cleaned up {killed} orphaned process(es)")
                else:
                    deps._stop_step("PID file pointed to non-gobby process, removed")
            return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    try:
        from gobby.runner_maintenance import write_shutdown_source
    except ImportError as exc:
        deps.logger.debug("Failed to write shutdown source: %s", exc)
    else:
        write_shutdown_source(shutdown_source, intent=shutdown_intent)

    stop_start = time.time()

    from gobby.cli.installers.service import get_service_status, service_stop

    svc = get_service_status()
    if svc.get("installed") and svc.get("running"):
        result = service_stop(
            shutdown_intent=shutdown_intent,
            shutdown_source=shutdown_source,
        )
        if result.get("success"):
            for _ in range(200):
                time.sleep(0.1)
                if not bool(deps._is_process_alive(pid)):
                    break
            deps.kill_all_gobby_daemons()
            if bool(deps._is_process_alive(pid)):
                if not quiet:
                    deps._stop_step(
                        "Service stop reported success but daemon is still running", error=True
                    )
                return False
            pid_file.unlink(missing_ok=True)
            elapsed = time.time() - stop_start
            if not quiet:
                deps._stop_step(f"Stopped via {svc.get('platform', 'OS')} service ({elapsed:.1f}s)")
            _report_lock_survivor(deps, quiet)
            return True
        if not quiet:
            deps._stop_step("Service stop failed, falling back to direct signal...", error=True)

    try:
        os.kill(pid, signal.SIGTERM)
        if not quiet:
            deps._stop_step(f"Sent shutdown signal (PID: {pid})")

        max_wait = 20
        for _ in range(max_wait * 10):
            time.sleep(0.1)
            if not bool(deps._is_process_alive(pid)):
                elapsed = time.time() - stop_start
                if not quiet:
                    deps._stop_step(f"Daemon stopped ({elapsed:.1f}s)")
                pid_file.unlink(missing_ok=True)
                deps.kill_all_gobby_daemons()
                _report_lock_survivor(deps, quiet)
                return True

        if not quiet:
            deps._stop_step(f"Did not stop within {max_wait}s, force killing...", error=True)

        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        except ProcessLookupError:
            pass

        if not bool(deps._is_process_alive(pid)):
            elapsed = time.time() - stop_start
            if not quiet:
                deps._stop_step(f"Force killed ({elapsed:.1f}s)")
            pid_file.unlink(missing_ok=True)
            deps.kill_all_gobby_daemons()
            _report_lock_survivor(deps, quiet)
            return True

        if not quiet:
            deps._stop_step("Failed to stop process", error=True)
        return False

    except PermissionError:
        if not quiet:
            deps._stop_step(f"Permission denied to stop process (PID {pid})", error=True)
        return False

    except ProcessLookupError:
        elapsed = time.time() - stop_start
        if not quiet:
            deps._stop_step(f"Daemon stopped ({elapsed:.1f}s)")
        pid_file.unlink(missing_ok=True)
        _report_lock_survivor(deps, quiet)
        return True

    except OSError as exc:
        if not quiet:
            deps._stop_step(f"Error stopping daemon: {exc}", error=True)
        return False
