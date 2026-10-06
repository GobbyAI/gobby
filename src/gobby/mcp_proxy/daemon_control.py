"""Daemon process control."""

import asyncio
import logging
import os
import signal
import sys
import tempfile
from typing import Any, Literal

import httpx
import psutil

from gobby.paths import get_gobby_home
from gobby.utils.env import is_test_protect_enabled

logger = logging.getLogger("gobby.daemon.control")

DaemonShutdownIntent = Literal["stop", "restart"]
DaemonShutdownSource = Literal["mcp_stop", "mcp_restart"]

# Above `gobby start`'s own health (120 s) and readiness (300 s) waits, leaving
# room for managed services and the schema apply.
DAEMON_START_TIMEOUT_SECONDS = 600.0
_START_OUTPUT_TAIL_CHARS = 2000


async def check_daemon_http_health(
    port: int,
    timeout: float = 5.0,
    *,
    base_url: str | None = None,
) -> bool:
    """Check if daemon is healthy via HTTP."""
    url = (base_url.rstrip("/") if base_url else f"http://localhost:{port}") + "/api/health"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=timeout)
            return resp.status_code == 200
    except Exception:
        return False


def get_daemon_pid() -> int | None:
    """Get PID of running daemon process.

    Under GOBBY_TEST_PROTECT, only return a PID whose cmdline references
    the current GOBBY_HOME / GOBBY_CONFIG_FILE — without this fence, this
    helper does a system-wide psutil scan and would return the user's
    production daemon PID, which any caller (e.g. stop_daemon_process)
    would then SIGTERM.
    """
    current_pid = os.getpid()
    test_protect = is_test_protect_enabled()
    home_marker = str(get_gobby_home()) if test_protect else None
    config_marker = os.environ.get("GOBBY_CONFIG_FILE") if test_protect else None

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] == current_pid:
                continue

            cmdline = proc.info["cmdline"]
            if not cmdline:
                continue

            cmdline_str = " ".join(cmdline)
            # Every start path (`gobby start`, service units) runs `-m gobby.runner`.
            if "gobby.runner" in cmdline_str:
                if test_protect:
                    in_home = home_marker is not None and home_marker in cmdline_str
                    in_config = config_marker is not None and config_marker in cmdline_str
                    if not (in_home or in_config):
                        continue

                from typing import cast

                return cast(int, proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return None


def is_daemon_running() -> bool:
    """Check if daemon is running."""
    return get_daemon_pid() is not None


async def _terminate_start_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate and reap a failed daemon-start child."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def start_daemon_process() -> dict[str, Any]:
    """Start the daemon with `gobby start` and wait for that command to exit.

    `gobby start` owns the singleton claim, managed services, the schema apply and
    the health and readiness waits; it exits 0 once the daemon is ready, and reads
    the same bootstrap ports the caller dials.
    """
    if is_daemon_running():
        pid = get_daemon_pid()
        return {
            "success": False,
            "already_running": True,
            "pid": pid,
            "message": f"Daemon is already running with PID {pid}",
        }

    cmd = [sys.executable, "-m", "gobby.cli", "start"]
    try:
        # A file, unlike a pipe, never holds the wait open for a grandchild, and the
        # caller's stdin and stdout carry the MCP stdio protocol.
        with tempfile.TemporaryFile() as output:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                returncode: int | None = await asyncio.wait_for(
                    proc.wait(), timeout=DAEMON_START_TIMEOUT_SECONDS
                )
            except TimeoutError:
                await _terminate_start_process(proc)
                returncode = None
            output.seek(0)
            tail = output.read().decode(errors="replace")[-_START_OUTPUT_TAIL_CHARS:].strip()
    except OSError as e:
        return {"success": False, "error": str(e), "message": f"Failed to start: {e}"}

    if returncode == 0:
        return {"success": True, "output": tail}
    outcome = (
        f"timed out after {DAEMON_START_TIMEOUT_SECONDS:.0f}s"
        if returncode is None
        else f"exited with code {returncode}"
    )
    return {
        "success": False,
        "message": f"gobby start {outcome}",
        "error": f"gobby start {outcome}: {tail}",
    }


async def stop_daemon_process(
    pid: int | None = None,
    *,
    shutdown_intent: DaemonShutdownIntent = "stop",
    shutdown_source: DaemonShutdownSource = "mcp_stop",
) -> dict[str, Any]:
    """Stop running daemon."""
    # SAFETY: never SIGTERM the production daemon during tests. Mirrors the
    # guard in gobby.cli.utils.stop_daemon. Without it, this code path can
    # reach the user's real daemon when a test (or test-spawned subprocess)
    # invokes mcp_proxy.daemon_control without going through the CLI.
    if is_test_protect_enabled():
        logger.warning("stop_daemon_process called during test - skipping")
        return {"success": True, "skipped": "test_protect"}

    if pid is None:
        pid = get_daemon_pid()

    if not pid:
        return {"success": False, "not_running": True, "message": "Daemon not running"}

    timeout = 5.0
    deadline = asyncio.get_running_loop().time() + timeout

    try:
        from gobby.runner_maintenance import write_shutdown_source

        try:
            write_shutdown_source(shutdown_source, intent=shutdown_intent)
        except Exception as e:
            logger.warning("Failed to write shutdown source: %s", e)
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
    stop_result = await stop_daemon_process(
        current_pid,
        shutdown_intent="restart",
        shutdown_source="mcp_restart",
    )
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

    return await start_daemon_process()
