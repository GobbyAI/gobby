"""Lifecycle endpoints for admin router."""

import asyncio
import logging
import os
import signal
import subprocess  # nosec B404 # subprocess needed for daemon restart
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from gobby.shutdown_intent import ShutdownIntent
from gobby.telemetry.instruments import inc_counter

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

_restart_lock: asyncio.Lock | None = None
_SERVICE_RESTART_HELPER = (
    "from gobby.servers.routes.admin._lifecycle import _run_service_restart_helper; "
    "import sys; _run_service_restart_helper(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3])"
)
_DIRECT_RESTART_HELPER = (
    "from gobby.servers.routes.admin._lifecycle import _run_direct_restart_helper; "
    "import sys; _run_direct_restart_helper(int(sys.argv[1]))"
)


def _get_restart_lock() -> asyncio.Lock:
    global _restart_lock
    if _restart_lock is None:
        _restart_lock = asyncio.Lock()
    return _restart_lock


def _wait_for_process_exit(pid: int, *, timeout: float, interval: float = 0.1) -> bool:
    """Wait for a process to disappear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(interval)
    return False


def _append_restart_helper_log(message: str) -> None:
    """Best-effort log for detached restart helpers."""
    try:
        log_dir = Path(os.environ.get("GOBBY_HOME", os.path.expanduser("~/.gobby"))) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with (log_dir / "admin-restart.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
    except Exception:
        logger.debug("Failed to write admin restart helper log", exc_info=True)


def _force_stop_process(pid: int) -> None:
    """Escalate from SIGTERM to SIGKILL for a stuck standalone daemon."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    if _wait_for_process_exit(pid, timeout=5.0):
        return

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return

    _wait_for_process_exit(pid, timeout=0.5)


def _run_service_restart_helper(current_pid: int, port: int, shutdown_source: str) -> None:
    """Finish a service-managed restart after the current daemon exits."""
    try:
        from gobby.cli.daemon import _wait_for_daemon_health
        from gobby.cli.installers.service import service_restart

        exited = _wait_for_process_exit(current_pid, timeout=30.0)

        # Avoid an unnecessary second restart when launchd/systemd already replaced us.
        if exited and _wait_for_daemon_health(port, timeout=3.0, interval=0.25) is not None:
            return

        result = service_restart(shutdown_source=shutdown_source)
        if not result.get("success"):
            _append_restart_helper_log(
                f"Admin restart service handoff failed: {result.get('error', 'unknown error')}"
            )
            return

        if _wait_for_daemon_health(port, timeout=30.0, interval=0.5) is None:
            _append_restart_helper_log(
                "Admin restart service handoff completed, but daemon never became healthy"
            )
    except Exception as exc:
        _append_restart_helper_log(f"Admin restart service helper crashed: {exc!r}")


def _run_direct_restart_helper(current_pid: int) -> None:
    """Finish a standalone restart by launching a fresh daemon process."""
    try:
        if not _wait_for_process_exit(current_pid, timeout=30.0):
            _force_stop_process(current_pid)

        # Give the old daemon time to release sockets/PID-file state before
        # the replacement process starts, or restart races can fail the handoff.
        time.sleep(2.0)

        gobby_home = Path(os.environ.get("GOBBY_HOME", os.path.expanduser("~/.gobby")))
        pid_file = gobby_home / "gobby.pid"
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass

        log_dir = gobby_home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (
            (log_dir / "gobby-client.log").open("a", encoding="utf-8") as log_file,
            (log_dir / "gobby-client-error.log").open("a", encoding="utf-8") as err_file,
        ):
            proc = subprocess.Popen(  # nosec B603
                [sys.executable, "-m", "gobby.runner"],
                stdout=log_file,
                stderr=err_file,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=os.environ.copy(),
            )
        pid_file.write_text(str(proc.pid), encoding="utf-8")
    except Exception as exc:
        _append_restart_helper_log(f"Admin restart direct helper crashed: {exc!r}")


def _should_restart_via_service_manager() -> bool:
    """Use the OS service manager when it owns the daemon."""
    try:
        from gobby.cli.installers.service import get_service_status

        status = get_service_status()
    except Exception:
        logger.warning("Failed to read service status for admin restart", exc_info=True)
        return False

    return bool(status.get("installed") and status.get("enabled"))


def _spawn_restart_helper(
    *,
    current_pid: int,
    port: int,
    service_managed: bool,
    shutdown_source: str,
) -> None:
    """Launch a detached helper that completes restart after this process exits."""
    helper = _SERVICE_RESTART_HELPER if service_managed else _DIRECT_RESTART_HELPER
    helper_args = (
        [str(current_pid), str(port), shutdown_source] if service_managed else [str(current_pid)]
    )
    subprocess.Popen(  # nosec B603
        [sys.executable, "-c", helper, *helper_args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )


def _request_runner_shutdown(server: "HTTPServer", intent: ShutdownIntent) -> bool:
    """Set the in-process runner shutdown state when this server owns one."""
    runner = getattr(server, "_runner", None)
    if runner is None:
        get_runner = getattr(server, "get_runner", None)
        runner = get_runner() if callable(get_runner) else None
    if runner is None:
        return False
    if callable(getattr(type(runner), "request_shutdown", None)):
        runner.request_shutdown(intent)
        return True
    runner._shutdown_intent = intent
    runner._shutdown_requested = True
    return True


def register_lifecycle_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.post("/shutdown")
    async def shutdown() -> dict[str, Any]:
        """
        Graceful daemon shutdown endpoint.

        Returns:
            Shutdown confirmation
        """
        start_time = time.perf_counter()
        inc_counter("shutdown_requests_total")

        try:
            logger.debug("Shutdown requested via HTTP endpoint")
            from gobby.runner_maintenance import write_shutdown_source

            write_shutdown_source("http_shutdown", intent="stop")
            runner_shutdown_requested = _request_runner_shutdown(server, ShutdownIntent.STOP)

            if not runner_shutdown_requested:
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
            logger.error(f"Error initiating shutdown: {e}", exc_info=True)
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

        restart_lock = _get_restart_lock()
        if restart_lock.locked():
            return {"status": "already_restarting", "message": "Restart already in progress"}

        try:
            await restart_lock.acquire()
            service_managed = _should_restart_via_service_manager()
            logger.info(
                "Restart requested via HTTP endpoint (service_managed=%s)",
                service_managed,
            )

            current_pid = os.getpid()

            _spawn_restart_helper(
                current_pid=current_pid,
                port=server.port,
                service_managed=service_managed,
                shutdown_source="http_restart",
            )

            from gobby.runner_maintenance import write_shutdown_source

            write_shutdown_source("http_restart", intent="restart")
            runner_shutdown_requested = _request_runner_shutdown(server, ShutdownIntent.RESTART)

            if not runner_shutdown_requested:
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

            logger.error(f"Error initiating restart: {e}", exc_info=True)
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
                logger.error(f"Failed to execute reload_cache: {e}")
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
            logger.error(f"Error reloading workflows: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
