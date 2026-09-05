"""Liveness probe plus admin status and metrics endpoints."""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import psutil
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from gobby.cli.services import is_qdrant_healthy
from gobby.hooks.runtime_compat import read_ghook_runtime_diagnostic
from gobby.paths import get_install_dir
from gobby.storage.hub.operation_deadline import database_operation_deadline
from gobby.telemetry.instruments import get_all_metrics, set_gauge, update_daemon_metrics

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

_STATUS_COLLECTION_BUDGET_SECONDS = 2.25
_STATUS_DB_OPERATION_TIMEOUT_SECONDS = 1.0
_STATUS_DEPENDENCY_TIMEOUT_SECONDS = 1.0
_STATUS_DEPENDENCY_TRANSPORT_TIMEOUT_SECONDS = 2.0


@dataclass
class _StatusCollection:
    values: dict[str, Any] = field(default_factory=dict)
    timed_out: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.timed_out and not self.failed

    def to_dict(self, *, budget_seconds: float) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "budget_seconds": budget_seconds,
            "timed_out": self.timed_out,
            "failed": self.failed,
        }


async def _collect_status_item(
    name: str,
    awaitable: Awaitable[Any],
    result: _StatusCollection,
) -> None:
    try:
        result.values[name] = await awaitable
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        result.timed_out.append(name)
        logger.warning("Status collector %s timed out", name)
    except Exception as exc:
        result.failed[name] = type(exc).__name__
        logger.warning(
            "Status collector %s failed: %s",
            name,
            type(exc).__name__,
            exc_info=True,
        )


async def _collect_status_items(
    collectors: dict[str, Awaitable[Any]],
    *,
    budget_seconds: float,
) -> _StatusCollection:
    """Collect independent status sections within one cancellation and DB budget."""
    result = _StatusCollection()
    try:
        with database_operation_deadline(
            timeout_seconds=budget_seconds,
            operation_timeout_seconds=min(
                budget_seconds,
                _STATUS_DB_OPERATION_TIMEOUT_SECONDS,
            ),
        ):
            async with asyncio.timeout(budget_seconds):
                async with asyncio.TaskGroup() as group:
                    for name, awaitable in collectors.items():
                        group.create_task(
                            _collect_status_item(name, awaitable, result),
                            name=f"gobby-status-{name}",
                        )
    except TimeoutError:
        result.timed_out = [
            name for name in collectors if name not in result.values and name not in result.failed
        ]
    return result


async def _collect_process_metrics() -> dict[str, Any]:
    process = psutil.Process(os.getpid())
    memory_info = process.memory_info()
    cpu_percent = await asyncio.to_thread(process.cpu_percent, 0.1)
    return {
        "memory_rss_mb": round(memory_info.rss / (1024 * 1024), 2),
        "memory_vms_mb": round(memory_info.vms / (1024 * 1024), 2),
        "cpu_percent": cpu_percent,
        "num_threads": process.num_threads(),
    }


async def _collect_session_stats(server: "HTTPServer") -> dict[str, Any]:
    status_counts = await server.run_db(server.session_manager.count_by_status)
    return {
        "total": sum(status_counts.values()),
        "active": status_counts.get("active", 0),
        "paused": status_counts.get("paused", 0),
        "awaiting_handoff": status_counts.get("awaiting_handoff", 0),
    }


async def _collect_task_stats(server: "HTTPServer") -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        state_counts = server.task_manager.count_by_state()
        stats = {
            key: state_counts.get(key, 0)
            for key in (
                "ready",
                "in_progress",
                "closed",
                "needs_review",
                "review_approved",
                "escalated",
            )
        }
        stats["ready_unblocked"] = server.task_manager.count_ready_tasks()
        stats["blocked"] = server.task_manager.count_blocked_tasks()
        stats["closed_24h"] = server.task_manager.count_closed_since(hours=24)
        return stats

    return cast(dict[str, Any], await server.run_db(collect))


async def _collect_memory_stats(server: "HTTPServer") -> dict[str, Any]:
    stats = await server.memory_manager.get_stats(include_vector_count=False)
    return {
        "count": stats.get("total_count", 0),
        "by_type": stats.get("by_type", {}),
        "recent_count": stats.get("recent_count", 0),
    }


async def _collect_qdrant_status(server: "HTTPServer") -> dict[str, Any]:
    vector_store = getattr(server.memory_manager, "_vector_store", None)
    qdrant_url = _get_qdrant_url(server, vector_store)
    status: dict[str, Any] = {
        "configured": vector_store is not None or qdrant_url is not None,
        "healthy": False,
    }
    snapshot: dict[str, Any] = {}
    if vector_store is not None:
        status_snapshot = getattr(vector_store, "status_snapshot", None)
        if callable(status_snapshot):
            candidate = status_snapshot()
            if isinstance(candidate, dict):
                snapshot = candidate

    if qdrant_url:
        async with asyncio.timeout(_STATUS_DEPENDENCY_TIMEOUT_SECONDS):
            status["healthy"] = await is_qdrant_healthy(
                qdrant_url,
                timeout=_STATUS_DEPENDENCY_TRANSPORT_TIMEOUT_SECONDS,
            )
    elif vector_store is not None:
        status["healthy"] = snapshot.get("state") in {
            "ready",
            "dimension_mismatch_pending_rebuild",
        }
        status["probe"] = "snapshot"
    status.update(snapshot)
    return status


async def _collect_pipeline_stats(server: "HTTPServer") -> dict[str, Any]:
    from gobby.storage.pipelines import LocalPipelineExecutionManager

    def collect() -> dict[str, Any]:
        manager = LocalPipelineExecutionManager(
            db=server.services.database,
            project_id=None,
        )
        status_counts = manager.count_by_status()
        stats = {
            key: status_counts.get(key, 0)
            for key in ("running", "waiting_approval", "completed", "failed")
        }
        stats["total"] = sum(stats.values())
        return stats

    return cast(dict[str, Any], await server.run_db(collect))


async def _collect_agent_stats(server: "HTTPServer") -> dict[str, int]:
    from gobby.storage.agents import LocalAgentRunManager

    def collect() -> list[Any]:
        return LocalAgentRunManager(server.services.database).list_running()

    return {"running": len(await server.run_db(collect))}


def _get_qdrant_url(server: "HTTPServer", vector_store: Any | None) -> str | None:
    """Resolve the configured Qdrant URL when available."""
    store_url = getattr(vector_store, "_url", None)
    if isinstance(store_url, str) and store_url:
        return store_url

    services = getattr(server, "services", None)
    config = getattr(services, "config", None) if services is not None else None
    databases = getattr(config, "databases", None) if config is not None else None
    qdrant = getattr(databases, "qdrant", None) if databases is not None else None
    config_url = getattr(qdrant, "url", None) if qdrant is not None else None
    return config_url if isinstance(config_url, str) and config_url else None


def _is_mcp_server_connected(mcp_manager: Any, name: str) -> bool:
    """Resolve connection state across concrete managers and lightweight test doubles."""
    is_connected = getattr(mcp_manager, "is_connected", None)
    if callable(is_connected):
        try:
            result = is_connected(name)
            if isinstance(result, bool):
                return result
        except Exception:
            logger.debug("MCP manager is_connected failed for %s", name, exc_info=True)

    connections = getattr(mcp_manager, "connections", {})
    if isinstance(connections, dict):
        return name in connections
    if isinstance(connections, (list, set, tuple)):
        return name in connections
    return False


def _is_postgres_runtime(server: "HTTPServer", database_status: dict[str, Any]) -> bool:
    """Return whether the active hub runtime is PostgreSQL."""
    backend = database_status.get("backend")
    if isinstance(backend, str) and backend == "postgres":
        return True

    services = getattr(server, "services", None)
    config = getattr(services, "config", None) if services is not None else None
    hub_backend = getattr(config, "hub_backend", None) if config is not None else None
    return isinstance(hub_backend, str) and hub_backend == "postgres"


async def _get_postgres_dashboard_status(
    server: "HTTPServer", database_status: dict[str, Any]
) -> dict[str, Any] | None:
    """Collect the PostgreSQL status payload used by the CLI status dashboard."""
    if not _is_postgres_runtime(server, database_status):
        return None

    try:
        from gobby.cli.installers.postgres import get_postgres_status

        return await get_postgres_status(
            database=server.services.database,
            run_db=server.run_db,
        )
    except Exception as exc:
        logger.warning("Failed to get PostgreSQL status: %s", type(exc).__name__)
        return {
            "available": False,
            "healthy": False,
            "error": type(exc).__name__,
        }


def _unavailable_falkordb_memory_status() -> dict[str, Any]:
    return {
        "configured": False,
        "installed": False,
        "healthy": False,
        "url": None,
    }


def _get_degraded_services(server: "HTTPServer") -> list[str]:
    """Return runner initialization degradations in stable display order."""
    runner = server.get_runner()
    if runner is None:
        return []
    degraded_services = getattr(runner, "degraded_services", None)
    if not isinstance(degraded_services, (set, list, tuple)):
        return []
    return sorted(str(service_name) for service_name in degraded_services)


def _gterm_host_status(server: "HTTPServer") -> dict[str, Any] | None:
    """Read-only gterm host health snapshot."""
    runner = server.get_runner()
    if runner is None:
        return None
    host = getattr(runner, "terminal_host_manager", None)
    health_state = getattr(host, "health_state", None)
    if not callable(health_state):
        return None
    snapshot = health_state()
    if isinstance(snapshot, dict):
        return snapshot
    return None


async def _get_falkordb_memory_status(server: "HTTPServer") -> dict[str, Any]:
    """Collect the FalkorDB status payload for the admin memory section."""
    try:
        from gobby.cli.services import get_falkordb_status
        from gobby.config.persistence import is_falkordb_enabled

        services = getattr(server, "services", None)
        daemon_config = getattr(server, "config", None) or getattr(services, "config", None)
        if daemon_config is None or services is None:
            raise RuntimeError("server config unavailable")
        database = getattr(services, "database", None)
        if database is None:
            raise RuntimeError("server database unavailable")

        falkor_cfg = daemon_config.databases.falkordb
        async with asyncio.timeout(_STATUS_DEPENDENCY_TIMEOUT_SECONDS):
            status = await get_falkordb_status(
                db=database,
                host=falkor_cfg.host,
                port=falkor_cfg.port,
                password=falkor_cfg.password,
                run_db=server.run_db,
                health_timeout=_STATUS_DEPENDENCY_TRANSPORT_TIMEOUT_SECONDS,
            )
        return {
            "configured": is_falkordb_enabled(daemon_config.databases),
            "installed": status["installed"],
            "healthy": status["healthy"],
            "url": status["url"],
        }
    except TimeoutError:
        raise
    except Exception as e:
        logger.warning(
            "Failed to check FalkorDB status: %s: %s",
            type(e).__name__,
            e,
        )
        return _unavailable_falkordb_memory_status()


def create_health_router(server: "HTTPServer") -> APIRouter:
    """Public liveness probe at ``/api/health``, mounted outside the admin prefix."""
    router = APIRouter(prefix="/api", tags=["health"])
    # The checkout this daemon serves bundled content from; `gobby sync` compares
    # its own checkout against it before overwriting the shared installed rows.
    install_dir = str(get_install_dir())

    @router.get("/health")
    async def health_check() -> dict[str, Any]:
        """Lightweight health check including local hook-runtime compatibility."""
        # Read the stamp inline. It is a few hundred bytes of page-cached local
        # JSON -- cheaper than the thread hop it used to pay for, and the hop
        # made liveness depend on a free slot in the shared default executor,
        # which is exactly what timed this endpoint out at five seconds
        # (#20839).
        hook_runtime = read_ghook_runtime_diagnostic()
        degraded_services = _get_degraded_services(server)
        payload: dict[str, Any] = {
            "status": "degraded" if hook_runtime.is_degraded or degraded_services else "ok",
            "degraded_services": degraded_services,
            "hook_runtime": hook_runtime.to_dict(),
            "install_dir": install_dir,
        }
        gterm_host = _gterm_host_status(server)
        if gterm_host is not None:
            payload["gterm_host"] = gterm_host
        return payload

    return router


def register_health_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.get("/startup-progress")
    async def startup_progress() -> dict[str, Any]:
        """Return subsystem initialization progress for CLI display."""
        from gobby.runner_lifecycle import get_startup_tracker

        tracker = get_startup_tracker()
        if tracker is None:
            return {
                "steps_completed": [],
                "steps_scheduled": [],
                "errors": [],
                "done": True,
                "elapsed_seconds": 0,
            }
        return tracker.to_dict()

    @router.get("/status")
    async def status_check() -> dict[str, Any]:
        """
        Comprehensive status check endpoint.

        Returns detailed health status including daemon state, uptime,
        memory usage, background tasks, and connection statistics.
        """
        start_time = time.perf_counter()
        hook_runtime = read_ghook_runtime_diagnostic()
        degraded_services = _get_degraded_services(server)

        # Get server uptime
        uptime_seconds = None
        if server._start_time is not None:
            uptime_seconds = time.time() - server._start_time

        # Get daemon status if available
        daemon_status = None
        if server._daemon is not None:
            try:
                daemon_status = server._daemon.status()
            except Exception as e:
                logger.warning("Failed to get daemon status: %s", e)

        process_metrics: dict[str, Any] | None = None

        # Get background task status
        all_metrics = get_all_metrics()
        counters = all_metrics.get("counters", {})
        background_tasks = {
            "active": len(server._background_tasks),
            "total": counters.get("background_tasks_total", {}).get("value", 0),
            "completed": counters.get("background_tasks_completed_total", {}).get("value", 0),
            "failed": counters.get("background_tasks_failed_total", {}).get("value", 0),
        }

        # Get MCP server status - include ALL configured servers
        mcp_health = {}
        if server.mcp_manager is not None:
            try:
                # Iterate over all configured servers, not just connected ones
                for config in server.mcp_manager.server_configs:
                    health = server.mcp_manager.health.get(config.name)
                    is_connected = _is_mcp_server_connected(server.mcp_manager, config.name)
                    mcp_health[config.name] = {
                        "connected": is_connected,
                        "status": (
                            health.state.value
                            if health
                            else ("connected" if is_connected else "not_started")
                        ),
                        "enabled": config.enabled,
                        "transport": config.transport,
                        "health": health.health.value if health else None,
                        "consecutive_failures": health.consecutive_failures if health else 0,
                        "last_health_check": (
                            health.last_health_check.isoformat()
                            if health and health.last_health_check
                            else None
                        ),
                        "response_time_ms": health.response_time_ms if health else None,
                        "last_error": health.last_error if health else None,
                        "tool_count": len(config.tools) if config.tools else 0,
                    }
            except Exception as e:
                logger.warning("Failed to get MCP health: %s", e)

        # Count internal tools from gobby-* registries and add them to mcp_health
        internal_tools_count = 0
        if server._internal_manager:
            for registry in server._internal_manager.get_all_registries():
                tools = registry.list_tools()
                internal_tools_count += len(tools)
                # Include internal servers in mcp_health for unified server count
                mcp_health[registry.name] = {
                    "connected": True,  # Internal servers are always available
                    "status": "connected",
                    "enabled": True,
                    "transport": "internal",
                    "health": "healthy",
                    "consecutive_failures": 0,
                    "last_health_check": None,
                    "response_time_ms": None,
                    "last_error": None,
                    "internal": True,  # Flag to distinguish from downstream servers
                    "tool_count": len(tools),
                }

        session_stats: dict[str, Any] = {
            "active": 0,
            "paused": 0,
            "awaiting_handoff": 0,
            "total": 0,
        }
        task_stats: dict[str, Any] = {
            "ready": 0,
            "in_progress": 0,
            "closed": 0,
            "needs_review": 0,
            "review_approved": 0,
            "escalated": 0,
            "ready_unblocked": 0,
            "blocked": 0,
            "closed_24h": 0,
        }
        vector_store = (
            getattr(server.memory_manager, "_vector_store", None)
            if server.memory_manager is not None
            else None
        )
        memory_stats: dict[str, Any] = {
            "count": 0,
            "by_type": {},
            "recent_count": 0,
            "qdrant": {
                "configured": bool(
                    vector_store is not None or _get_qdrant_url(server, vector_store)
                ),
                "healthy": False,
            },
            "falkordb": _unavailable_falkordb_memory_status(),
        }
        pipeline_stats: dict[str, Any] = {
            "running": 0,
            "waiting_approval": 0,
            "completed": 0,
            "failed": 0,
            "total": 0,
        }
        skills_stats: dict[str, Any] = {"total": 0}

        # Compute total cached tools across downstream servers
        downstream_tools_count = 0
        if server.mcp_manager:
            for config in server.mcp_manager.server_configs:
                if config.tools:
                    downstream_tools_count += len(config.tools)

        # File descriptor usage
        fd_usage: dict[str, Any] = {}
        try:
            import resource

            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            # Count open FDs via /dev/fd or /proc
            import pathlib

            fd_dir = pathlib.Path("/dev/fd")
            if not fd_dir.exists():
                fd_dir = pathlib.Path(f"/proc/{os.getpid()}/fd")
            current = len(list(fd_dir.iterdir())) if fd_dir.exists() else None
            fd_usage = {"current": current, "soft_limit": soft, "hard_limit": hard}
        except Exception:
            logger.debug("Could not collect file descriptor usage", exc_info=True)

        # Last shutdown source
        last_shutdown: str | None = None
        try:
            from gobby.cli.utils import get_gobby_home as _ghome2
            from gobby.shutdown_intent import (
                format_shutdown_source,
                read_active_shutdown_intent,
                read_shutdown_source_record,
            )

            home = _ghome2()
            shutdown_record = read_shutdown_source_record(home=home)
            if shutdown_record is None:
                shutdown_record = read_active_shutdown_intent(home=home, max_age_seconds=120)
            if shutdown_record is not None:
                last_shutdown = format_shutdown_source(shutdown_record)
        except Exception:
            logger.debug("Could not read the last shutdown source", exc_info=True)

        agent_stats: dict[str, int] = {"running": 0}

        provider_model_status = {}
        provider_capability_service = getattr(server.services, "provider_capability_service", None)
        if provider_capability_service is not None:
            try:
                provider_model_status = {
                    snapshot.provider: {
                        "generation": snapshot.generation,
                        "model_count": len(snapshot.models),
                        "sources": [source.to_dict() for source in snapshot.sources],
                    }
                    for snapshot in provider_capability_service.get_all_snapshots()
                }
            except Exception as e:
                logger.warning("Failed to get provider capability status: %s", e)

        database_status: dict[str, Any] = {}
        db_size_bytes: int | None = None
        db = getattr(server.services, "database", None)
        if db is not None:
            database_status["backend"] = getattr(db, "dialect", None)
            connection_count = getattr(db, "connection_count", None)
            database_status["connection_count"] = (
                connection_count if isinstance(connection_count, int) else None
            )
            db_path = getattr(db, "db_path", None)
            try:
                if db_path is not None:
                    resolved_db_path = Path(db_path).expanduser()
                    if resolved_db_path.exists():
                        db_size_bytes = resolved_db_path.stat().st_size
            except Exception:
                logger.debug("Could not read database file size", exc_info=True)
        executor_stats = server.services.db_executor_stats()
        if executor_stats is not None:
            database_status["executor"] = executor_stats
        database_watchdog = getattr(server.services, "database_watchdog", None)
        if database_watchdog is not None:
            try:
                database_status["concurrency"] = database_watchdog.status_snapshot()
            except Exception as exc:
                logger.warning("Failed to collect database concurrency status: %s", exc)

        collectors: dict[str, Awaitable[Any]] = {
            "process": _collect_process_metrics(),
            "pipelines": _collect_pipeline_stats(server),
            "agents": _collect_agent_stats(server),
        }
        if server.session_manager is not None:
            collectors["sessions"] = _collect_session_stats(server)
        if server.task_manager is not None:
            collectors["tasks"] = _collect_task_stats(server)
        if server.memory_manager is not None:
            collectors["memory"] = _collect_memory_stats(server)
            collectors["qdrant"] = _collect_qdrant_status(server)
            collectors["falkordb"] = _get_falkordb_memory_status(server)
        if server.skill_manager is not None:
            collectors["skills"] = server.run_db(server.skill_manager.count_skills)
        if _is_postgres_runtime(server, database_status):
            collectors["postgres"] = _get_postgres_dashboard_status(server, database_status)

        remaining_collection_budget = max(
            0.001,
            _STATUS_COLLECTION_BUDGET_SECONDS - (time.perf_counter() - start_time),
        )
        collection = await _collect_status_items(
            collectors,
            budget_seconds=remaining_collection_budget,
        )
        process_metrics = collection.values.get("process")
        session_stats.update(collection.values.get("sessions", {}))
        task_stats.update(collection.values.get("tasks", {}))
        memory_stats.update(collection.values.get("memory", {}))
        memory_stats["qdrant"] = collection.values.get("qdrant", memory_stats["qdrant"])
        memory_stats["falkordb"] = collection.values.get("falkordb", memory_stats["falkordb"])
        pipeline_stats.update(collection.values.get("pipelines", {}))
        if "skills" in collection.values:
            skills_stats["total"] = collection.values["skills"]
        agent_stats.update(collection.values.get("agents", {}))

        postgres_status = collection.values.get("postgres")
        if "postgres" in collection.timed_out:
            postgres_status = {
                "available": False,
                "healthy": False,
                "error": "status collection timed out",
            }
        elif "postgres" in collection.failed:
            postgres_status = {
                "available": False,
                "healthy": False,
                "error": collection.failed["postgres"],
            }

        postgres_healthy = True
        postgres_code_index_healthy = True
        if postgres_status is not None:
            postgres_healthy = bool(postgres_status.get("healthy"))
            code_index_status = postgres_status.get("code_index")
            if isinstance(code_index_status, dict):
                postgres_code_index_healthy = bool(code_index_status.get("healthy"))
        automation_loop = getattr(server.services, "system_automation_loop", None)
        system_services: dict[str, Any] = {}
        if automation_loop is not None:
            try:
                system_services["automation_loop"] = automation_loop.status_snapshot()
            except Exception as e:
                logger.warning("Failed to get automation loop status: %s", e)

        endpoint_health = getattr(server.services, "generation_endpoint_health", None)
        generation_endpoints = endpoint_health.snapshot() if endpoint_health is not None else []
        response_time_ms = (time.perf_counter() - start_time) * 1000

        payload: dict[str, Any] = {
            "status": (
                "healthy"
                if (
                    server._running
                    and not hook_runtime.is_degraded
                    and not degraded_services
                    and collection.complete
                    and postgres_healthy
                    and postgres_code_index_healthy
                )
                else "degraded"
            ),
            "degraded_services": degraded_services,
            "dev_mode": getattr(server.services, "dev_mode", False),
            "project_id": getattr(server.services, "project_id", None),
            "server": {
                "port": server.port,
                "test_mode": server.test_mode,
                "running": server._running,
                "uptime_seconds": uptime_seconds,
            },
            "daemon": daemon_status,
            "process": process_metrics,
            "background_tasks": background_tasks,
            "mcp_servers": mcp_health,
            "internal_tools_count": internal_tools_count,
            "mcp_tools_cached": internal_tools_count + downstream_tools_count,
            "sessions": session_stats,
            "tasks": task_stats,
            "memory": memory_stats,
            "skills": skills_stats,
            "pipelines": pipeline_stats,
            "provider_models": provider_model_status,
            "generation_endpoints": generation_endpoints,
            "database": database_status,
            "system_services": system_services,
            "agents": agent_stats,
            "fd_usage": fd_usage,
            "db_size_bytes": db_size_bytes,
            "last_shutdown": last_shutdown,
            "hook_runtime": hook_runtime.to_dict(),
            "response_time_ms": response_time_ms,
            "status_collection": collection.to_dict(
                budget_seconds=_STATUS_COLLECTION_BUDGET_SECONDS
            ),
        }
        gterm_host = _gterm_host_status(server)
        if gterm_host is not None:
            payload["gterm_host"] = gterm_host
        if postgres_status is not None:
            payload["postgres"] = postgres_status
        return payload

    @router.get("/metrics")
    async def get_metrics() -> PlainTextResponse:
        """
        Prometheus-compatible metrics endpoint.

        Returns metrics in Prometheus text exposition format including:
        - HTTP request counts and durations
        - Background task metrics
        - Daemon health metrics
        """
        try:
            # Update daemon health metrics
            update_daemon_metrics()

            # Update background task gauge
            set_gauge("background_tasks_active", float(len(server._background_tasks)))

            # Export in Prometheus format using prometheus_client integration
            return PlainTextResponse(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

        except Exception as e:
            logger.exception("Failed to export metrics: %s", e)
            raise
