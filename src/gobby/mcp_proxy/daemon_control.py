"""Daemon process control."""

import asyncio
import logging
import os
import signal
import subprocess
import sys
from typing import Any

import httpx
import psutil

logger = logging.getLogger("gobby.daemon.control")


async def check_daemon_http_health(port: int, timeout: float = 2.0) -> bool:
    """Check if daemon is healthy via HTTP."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://localhost:{port}/health", timeout=timeout)
            return resp.status_code == 200
    except Exception:
        return False


def get_daemon_pid() -> int | None:
    """Get PID of running daemon process."""
    current_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] == current_pid:
                continue

            cmdline = proc.info["cmdline"]
            if (
                cmdline
                and "gobby.cli.app" in cmdline
                and "daemon" in cmdline
                and "start" in cmdline
            ):
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return None


def is_daemon_running() -> bool:
    """Check if daemon is running."""
    return get_daemon_pid() is not None


async def start_daemon_process(port: int, websocket_port: int) -> dict[str, Any]:
    """Start daemon in a new process."""
    if is_daemon_running():
        pid = get_daemon_pid()
        return {
            "success": False,
            "already_running": True,
            "pid": pid,
            "message": f"Daemon is already running with PID {pid}",
        }

    cmd = [
        sys.executable,
        "-m",
        "gobby.cli.app",
        "daemon",
        "start",
        "--port",
        str(port),
        "--websocket-port",
        str(websocket_port),
    ]

    try:
        # Using subprocess.run as seemingly expected by tests / handling logic
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

        if result.returncode == 0:
            # Allow some time for startup if needed, though run waits for process exit if it doesn't daemonize.
            # Assuming "gobby daemon start" prints "Daemon started" and exits (forking).
            await asyncio.sleep(0.5)
            pid = get_daemon_pid()
            return {
                "success": True,
                "pid": pid,
                "output": result.stdout.strip() or "Daemon started",
            }
        else:
            return {
                "success": False,
                "message": "Start failed",
                "error": result.stderr or "Unknown error",
            }

    except Exception as e:
        return {"success": False, "error": str(e), "message": f"Failed to start: {e}"}


async def stop_daemon_process(pid: int | None = None) -> dict[str, Any]:
    """Stop running daemon."""
    if pid is None:
        pid = get_daemon_pid()

    if not pid:
        return {"success": False, "not_running": True, "message": "Daemon not running"}

    try:
        os.kill(pid, signal.SIGTERM)
        return {"success": True, "output": "Daemon stopped"}
    except ProcessLookupError:
        return {"success": False, "error": "Process not found", "not_running": True}
    except PermissionError:
        return {"success": False, "error": "Permission denied"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def restart_daemon_process(
    current_pid: int | None, port: int, websocket_port: int
) -> dict[str, Any]:
    """Restart daemon."""
    await stop_daemon_process(current_pid)
    await asyncio.sleep(1)
    return await start_daemon_process(port, websocket_port)
