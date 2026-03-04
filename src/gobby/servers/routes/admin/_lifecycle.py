"""Lifecycle endpoints for admin router."""

import asyncio
import logging
import os
import subprocess  # nosec B404 - subprocess needed for daemon restart
import sys
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from gobby.utils.metrics import get_metrics_collector

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

_restart_lock: asyncio.Lock | None = None


def _get_restart_lock() -> asyncio.Lock:
    global _restart_lock
    if _restart_lock is None:
        _restart_lock = asyncio.Lock()
    return _restart_lock


def register_lifecycle_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.post("/shutdown")
    async def shutdown() -> dict[str, Any]:
        """
        Graceful daemon shutdown endpoint.

        Returns:
            Shutdown confirmation
        """
        start_time = time.perf_counter()
        metrics = get_metrics_collector()

        metrics.inc_counter("http_requests_total")
        metrics.inc_counter("shutdown_requests_total")

        try:
            logger.debug("Shutdown requested via HTTP endpoint")

            # Create background task for shutdown
            task = asyncio.create_task(server._process_shutdown())

            server._background_tasks.add(task)
            task.add_done_callback(server._background_tasks.discard)

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "shutting_down",
                "message": "Graceful shutdown initiated",
                "response_time_ms": response_time_ms,
            }

        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error("Error initiating shutdown: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": "Shutdown failed to initiate",
            }

    @router.post("/restart")
    async def restart() -> dict[str, Any]:
        """
        Graceful daemon restart endpoint.

        Spawns a detached restarter subprocess that waits for the current
        daemon to exit, then starts a new one. Returns immediately.
        """
        start_time = time.perf_counter()
        metrics = get_metrics_collector()
        metrics.inc_counter("http_requests_total")

        restart_lock = _get_restart_lock()
        if restart_lock.locked():
            return {"status": "already_restarting", "message": "Restart already in progress"}

        try:
            await restart_lock.acquire()
            logger.info("Restart requested via HTTP endpoint")

            current_pid = os.getpid()
            port = server.port

            # Write shutdown source in the parent before spawning restarter
            from gobby.runner_maintenance import write_shutdown_source

            write_shutdown_source("http_restart")

            # Inline Python script for the detached restarter process.
            # It waits for the current daemon PID to exit, then spawns a new one.
            restarter_script = f"""
import os, sys, time, signal, subprocess
pid = {current_pid}
port = {port}
python = {sys.executable!r}

# Wait for current daemon to exit (up to 30s)
for _ in range(300):
    try:
        os.kill(pid, 0)
        time.sleep(0.1)
    except ProcessLookupError:
        break
else:
    # Graceful stop: SIGTERM first, then SIGKILL after 5s
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        for _ in range(50):  # 5s grace
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                break
        else:
            try:
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.5)
            except ProcessLookupError:
                pass

# Wait for port release
time.sleep(2.0)

# Clean up stale PID file
gobby_home = os.environ.get("GOBBY_HOME", os.path.expanduser("~/.gobby"))
pid_file = os.path.join(gobby_home, "gobby.pid")
try:
    os.unlink(pid_file)
except FileNotFoundError:
    pass

# Start new daemon
import json
log_dir = os.path.join(gobby_home, "logs")
os.makedirs(log_dir, exist_ok=True)
with open(os.path.join(log_dir, "gobby-client.log"), "a") as log_file, \\
     open(os.path.join(log_dir, "gobby-client-error.log"), "a") as err_file:
    proc = subprocess.Popen(
        [python, "-m", "gobby.runner"],
        stdout=log_file, stderr=err_file,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )
with open(pid_file, "w") as f:
    f.write(str(proc.pid))
"""
            # Spawn the restarter as a fully detached subprocess
            subprocess.Popen(  # nosec B603
                [sys.executable, "-c", restarter_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=os.environ.copy(),
            )

            # Schedule shutdown of the current daemon
            task = asyncio.create_task(server._process_shutdown())
            server._background_tasks.add(task)
            task.add_done_callback(server._background_tasks.discard)

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "restarting",
                "message": "Daemon restart initiated",
                "response_time_ms": response_time_ms,
            }

        except Exception as e:
            restart_lock.release()
            metrics.inc_counter("http_requests_errors_total")
            logger.error("Error initiating restart: %s", e, exc_info=True)
            return {
                "status": "error",
                "message": f"Restart failed to initiate: {e}",
            }

    @router.post("/workflows/reload")
    async def reload_workflows() -> dict[str, Any]:
        """
        Reload workflow definitions from disk.

        Triggers the gobby-workflows.reload_cache MCP tool internally.
        """
        start_time = time.perf_counter()
        metrics = get_metrics_collector()
        metrics.inc_counter("http_requests_total")

        try:
            # Find the gobby-workflows registry
            workflows_registry = None
            if server._internal_manager:
                for registry in server._internal_manager.get_all_registries():
                    if registry.name == "gobby-workflows":
                        workflows_registry = registry
                        break

            if not workflows_registry:
                return {
                    "status": "error",
                    "message": "Workflow registry not available",
                }

            # Call reload_cache tool directly via registry.call which handles async/sync
            try:
                result = await workflows_registry.call("reload_cache", {})
            except ValueError:
                return {
                    "status": "error",
                    "message": "reload_cache tool not found",
                }
            except Exception as e:
                logger.error("Failed to execute reload_cache: %s", e)
                return {
                    "status": "error",
                    "message": f"Failed to reload cache: {e}",
                }

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "message": "Workflow cache reloaded",
                "details": result,
                "response_time_ms": response_time_ms,
            }

        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error("Error reloading workflows: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
