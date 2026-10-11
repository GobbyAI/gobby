"""Lifecycle endpoints for admin router."""

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Response

from gobby.sessions.handoff_shutdown import (
    HandoffShutdownBlocked,
    cancel_handoff_shutdown,
    prepare_handoff_shutdown,
)
from gobby.shutdown_intent import ShutdownIntent, shutdown_marker_details
from gobby.telemetry.instruments import inc_counter
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

_restart_lock: asyncio.Lock | None = None


def _get_restart_lock() -> asyncio.Lock:
    global _restart_lock
    if _restart_lock is None:
        _restart_lock = asyncio.Lock()
    return _restart_lock


def _request_runner_shutdown(
    server: "HTTPServer",
    intent: ShutdownIntent,
    *,
    drain_terminals: bool = False,
) -> bool:
    """Set the in-process runner shutdown state when this server owns one."""
    runner = getattr(server, "_runner", None)
    if runner is None:
        get_runner = getattr(server, "get_runner", None)
        runner = get_runner() if callable(get_runner) else None
    if runner is None:
        return False
    request_shutdown = getattr(type(runner), "request_shutdown", None)
    if not callable(request_shutdown):
        return False
    if drain_terminals:
        runner.request_shutdown(intent, drain_terminals=True)
    else:
        runner.request_shutdown(intent)
    return True


async def _prepare_handoff_shutdown(server: "HTTPServer", machine_id: str) -> None:
    """Finish or undo admission if the HTTP caller disconnects during the DB check."""
    if server.session_manager is None:
        raise RuntimeError("Session storage unavailable; cannot check pending handoffs")
    pending = asyncio.create_task(
        server.run_db(prepare_handoff_shutdown, server.session_manager.db, machine_id)
    )
    try:
        await asyncio.shield(pending)
    except asyncio.CancelledError:
        try:
            await pending
        except Exception:
            pass  # Admission was never granted; preserve caller cancellation.
        else:
            cancel_handoff_shutdown(machine_id)
        raise


def register_lifecycle_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.get("/cron/protected-runs")
    async def protected_cron_runs() -> dict[str, Any]:
        """Active restart-protected cron runs — the restart lease `gobby stop` honors."""
        runner = server.get_runner()
        if runner is None:
            raise HTTPException(status_code=503, detail="runner unavailable")
        scheduler = runner.cron_scheduler
        if scheduler is None:
            return {"runs": []}
        runs = await server.run_db(scheduler.list_protected_runs)
        return {"runs": runs}

    @router.post("/shutdown")
    async def shutdown(
        response: Response,
        terminals: bool = False,
        force: bool = False,
        restart_supervisor: bool = False,
    ) -> dict[str, Any]:
        """
        Graceful daemon shutdown endpoint.

        ``terminals=true`` also drains the gterm host; by default it and its
        native terminals survive the daemon (#22002).

        Returns:
            Shutdown confirmation
        """
        start_time = time.perf_counter()
        inc_counter("shutdown_requests_total")
        machine_id = require_machine_id()
        shutdown_initiated = False
        admission_prepared = False

        try:
            if not force:
                runner = server.get_runner()
                scheduler = getattr(runner, "cron_scheduler", None) if runner else None
                if scheduler is not None:
                    protected_runs = await server.run_db(scheduler.list_protected_runs)
                    if protected_runs:
                        response.status_code = 409
                        return {
                            "status": "restart_protected",
                            "message": "Shutdown blocked by active protected cron runs",
                            "protected_runs": protected_runs,
                        }
                await _prepare_handoff_shutdown(server, machine_id)
                admission_prepared = True
            logger.debug("Shutdown requested via HTTP endpoint")
            from gobby.runner_maintenance import write_shutdown_source

            intent = "restart" if restart_supervisor else "stop"
            if terminals or restart_supervisor:
                write_shutdown_source(
                    "http_shutdown",
                    intent=intent,
                    details=shutdown_marker_details(
                        drain_terminals=terminals, restart_supervisor=restart_supervisor
                    ),
                )
            else:
                write_shutdown_source("http_shutdown", intent="stop")
            runner_shutdown_requested = _request_runner_shutdown(
                server, ShutdownIntent.STOP, drain_terminals=terminals
            )

            if not runner_shutdown_requested:
                task = asyncio.create_task(server._process_shutdown())
                server._background_tasks.add(task)
                task.add_done_callback(server._background_tasks.discard)
            shutdown_initiated = True

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "shutting_down",
                "message": "Graceful shutdown initiated",
                "response_time_ms": response_time_ms,
            }

        except HandoffShutdownBlocked as e:
            response.status_code = 409
            return {"status": "handoff_pending", "message": str(e)}
        except Exception as e:
            logger.exception("Error initiating shutdown: %s", e)
            response.status_code = 500
            return {
                "status": "error",
                "message": "Shutdown failed to initiate",
            }
        finally:
            if admission_prepared and not shutdown_initiated:
                cancel_handoff_shutdown(machine_id)

    @router.post("/restart")
    async def restart(
        response: Response,
        force: bool = False,
        terminals: bool = False,
    ) -> dict[str, Any]:
        """
        Graceful daemon restart endpoint.

        Requests backend shutdown with restart intent. The native supervisor
        reaps and replaces this backend. Returns immediately.
        ``terminals=true`` also restarts the gterm host (#22002).
        """
        start_time = time.perf_counter()

        restart_lock = _get_restart_lock()
        if restart_lock.locked():
            return {"status": "already_restarting", "message": "Restart already in progress"}

        shutdown_initiated = False
        machine_id = require_machine_id()
        admission_prepared = False
        try:
            await restart_lock.acquire()

            if not force:
                runner = server.get_runner()
                scheduler = getattr(runner, "cron_scheduler", None) if runner else None
                if scheduler is not None:
                    protected_runs = await server.run_db(scheduler.list_protected_runs)
                    if protected_runs:
                        response.status_code = 409
                        restart_lock.release()
                        return {
                            "status": "restart_protected",
                            "message": "Restart blocked by active protected cron runs",
                            "protected_runs": protected_runs,
                        }

            if not force:
                await _prepare_handoff_shutdown(server, machine_id)
                admission_prepared = True

            logger.info("Backend restart requested via HTTP endpoint")

            from gobby.runner_maintenance import write_shutdown_source

            if terminals:
                write_shutdown_source(
                    "http_restart",
                    intent="restart",
                    details=shutdown_marker_details(drain_terminals=True),
                )
            else:
                write_shutdown_source("http_restart", intent="restart")
            runner_shutdown_requested = _request_runner_shutdown(
                server, ShutdownIntent.RESTART, drain_terminals=terminals
            )

            if runner_shutdown_requested:
                shutdown_initiated = True
            else:
                task = asyncio.create_task(server._process_shutdown())
                server._background_tasks.add(task)
                task.add_done_callback(server._background_tasks.discard)
                shutdown_initiated = True

            if os.name == "nt":
                from gobby.runner_windows_restart import spawn_restart_helper

                await asyncio.to_thread(spawn_restart_helper, os.getpid(), server.port)
            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "restarting",
                "message": "Daemon restart initiated",
                "backend_pid": os.getpid(),
                "response_time_ms": response_time_ms,
            }

        except HandoffShutdownBlocked as e:
            if restart_lock.locked():
                restart_lock.release()
            response.status_code = 409
            return {"status": "handoff_pending", "message": str(e)}
        except Exception as e:
            if not shutdown_initiated and restart_lock.locked():
                restart_lock.release()

            logger.exception("Error initiating restart: %s", e)
            return {
                "status": "error",
                "message": "Restart failed to initiate",
            }
        finally:
            if admission_prepared and not shutdown_initiated:
                cancel_handoff_shutdown(machine_id)
            if not shutdown_initiated and restart_lock.locked():
                restart_lock.release()

    @router.post("/workflows/reload")
    async def reload_workflows(
        response: Response,
        project_path: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
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
                response.status_code = 503
                return {
                    "status": "error",
                    "message": "Workflow registry not available",
                }

            # Call reload_cache tool directly via registry.call which handles async/sync
            try:
                args = {
                    key: value
                    for key, value in {
                        "project_path": project_path,
                        "project_id": project_id,
                    }.items()
                    if value is not None
                }
                result = await workflows_registry.call("reload_cache", args)
            except ValueError:
                response.status_code = 503
                return {
                    "status": "error",
                    "message": "reload_cache tool not found",
                }
            except Exception as e:
                logger.exception(
                    "Failed to execute reload_cache",
                    extra={"error": str(e)},
                )
                response.status_code = 500
                return {
                    "status": "error",
                    "message": "Failed to reload cache",
                }

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "message": "Workflow cache reloaded",
                "details": result,
                "response_time_ms": response_time_ms,
            }

        except Exception as e:
            logger.exception(
                "Error reloading workflows",
                extra={"error": str(e)},
            )
            response.status_code = 500
            return {
                "status": "error",
                "message": "Failed to reload workflows",
            }
