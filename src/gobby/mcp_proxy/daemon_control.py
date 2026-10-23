"""Daemon process control."""

import asyncio
import logging
import os
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

# Above `gobby start`'s own health (300 s) and readiness (300 s) waits, leaving
# room for managed services and the schema apply.
DAEMON_START_TIMEOUT_SECONDS = 900.0
_START_OUTPUT_TAIL_CHARS = 2000


async def check_daemon_http_health(
    port: int,
    timeout: float = 5.0,
    *,
    base_url: str | None = None,
    tolerate_busy: bool = False,
) -> bool:
    """Check whether the daemon is up via HTTP.

    Startup readiness requires HTTP 200. Proxy preflight may explicitly accept
    a read, connect or pool timeout: under fleet load the daemon can be up and busy
    while its health endpoint takes seconds to answer or accept a connection (#23080, #23650).
    """
    url = (base_url.rstrip("/") if base_url else f"http://localhost:{port}") + "/api/health"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=timeout)
            return resp.status_code == 200
    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout):
        return tolerate_busy
    except Exception:
        return False


def get_daemon_pid() -> int | None:
    """Get PID of running daemon process.

    Under GOBBY_TEST_PROTECT, only return a PID whose cmdline references
    the current GOBBY_HOME / GOBBY_CONFIG_FILE — without this fence, this
    helper does a system-wide psutil scan and would return the user's
    production daemon PID, which any caller (e.g. stop_daemon) would then
    SIGTERM.
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
