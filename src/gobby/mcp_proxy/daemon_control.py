"""Daemon process control."""

import asyncio
import logging
import os
import signal
import sys
from typing import Any

import httpx
import psutil

logger = logging.getLogger("gobby.daemon.control")


async def check_daemon_http_health(port: int, timeout: float = 2.0) -> bool:
    """Check if daemon is healthy via HTTP."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://localhost:{port}/api/admin/health", timeout=timeout)
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
            if not cmdline:
                continue

            cmdline_str = " ".join(cmdline)
            # Match either gobby.runner or gobby.cli daemon start
            if "gobby.runner" in cmdline_str or (
                "gobby.cli" in cmdline_str and "daemon" in cmdline_str
            ):
                from typing import cast

                return cast(int, proc.info["pid"])
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
        # Use asyncio.create_subprocess_exec to avoid blocking the event loop
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Do NOT await communicate() - this blocks until exit.
        # Instead, wait a brief moment to check for immediate crash.
        await asyncio.sleep(0.5)

        if proc.returncode is not None:
            # Process exited immediately - capture output
            stdout, stderr = await proc.communicate()
            return {
                "success": False,
                "message": "Start failed - process exited immediately",
                "error": stderr.decode().strip() if stderr else "Unknown error",
            }

        # Process is running - check health
        if await check_daemon_http_health(port, timeout=5.0):
            return {
                "success": True,
                "pid": proc.pid,
                "output": "Daemon started successfully",
            }

        # If health check fails but process is still running, check pid directly
        # This might happen if listening takes longer than health check
        pid = get_daemon_pid()
        if pid:
            return {
                "success": True,
                "pid": pid,
                "output": "Daemon started (health check pending)",
            }

        return {
            "success": False,
            "message": "Start failed - process running but unhealthy",
            "error": "Health check timed out",
        }

    except Exception as e:
        return {"success": False, "error": str(e), "message": f"Failed to start: {e}"}


async def stop_daemon_process(pid: int | None = None) -> dict[str, Any]:
    """Stop running daemon."""
    if pid is None:
        pid = get_daemon_pid()

    if not pid:
        return {"success": False, "not_running": True, "message": "Daemon not running"}

    timeout = 5.0
    deadline = asyncio.get_running_loop().time() + timeout

    try:
        os.kill(pid, signal.SIGTERM)

        # Poll for termination
        while True:
            try:
                os.kill(pid, 0)
                if asyncio.get_running_loop().time() > deadline:
                    return {
                        "success": False,
                        "error": "Process did not exit after SIGTERM",
                        "message": "Stop timed out",
                    }
                await asyncio.sleep(0.1)
            except ProcessLookupError:
                # Process is gone
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
    stop_result = await stop_daemon_process(current_pid)
    if not stop_result.get("success") and not stop_result.get("not_running"):
        return stop_result

    # Wait for ports to be free with actual port checking
    import socket

    def is_port_free(p: int) -> bool:
        """Check if a port is available by attempting to bind to it."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", p))
                return True
        except OSError:
            return False

    for _ in range(10):
        if await asyncio.to_thread(is_port_free, port) and await asyncio.to_thread(
            is_port_free, websocket_port
        ):
            break
        await asyncio.sleep(0.5)
    else:
        return {
            "success": False,
            "error": f"Ports {port} and/or {websocket_port} not free after 10 retries",
        }

    return await start_daemon_process(port, websocket_port)
