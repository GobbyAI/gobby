"""Liveness probe plus admin status and metrics endpoints."""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from gobby.cli.services import is_qdrant_healthy
from gobby.hooks.runtime_compat import read_ghook_runtime_diagnostic
from gobby.telemetry.instruments import get_all_metrics, set_gauge, update_daemon_metrics

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


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

        return await get_postgres_status(readiness_timeout=1.5, connect_timeout=1)
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
        status = await get_falkordb_status(
            db=database,
            host=falkor_cfg.host,
            port=falkor_cfg.port,
            password=falkor_cfg.password,
        )
        return {
            "configured": is_falkordb_enabled(daemon_config.databases),
            "installed": status["installed"],
            "healthy": status["healthy"],
            "url": status["url"],
        }
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
        return {
            "status": "degraded" if hook_runtime.is_degraded or degraded_services else "ok",
            "degraded_services": degraded_services,
            "hook_runtime": hook_runtime.to_dict(),
        }

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

        # Get process metrics
        try:
            process = psutil.Process(os.getpid())
            memory_info = process.memory_info()
            # Run cpu_percent in a thread executor to avoid blocking the event loop
            # (interval=0.1 would block for 100ms otherwise)
            cpu_percent = await asyncio.to_thread(process.cpu_percent, 0.1)

            process_metrics = {
                "memory_rss_mb": round(memory_info.rss / (1024 * 1024), 2),
                "memory_vms_mb": round(memory_info.vms / (1024 * 1024), 2),
                "cpu_percent": cpu_percent,
                "num_threads": process.num_threads(),
            }
        except Exception as e:
            logger.warning("Failed to get process metrics: %s", e)
            process_metrics = None

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

        # Get session statistics using efficient count queries
        session_stats: dict[str, Any] = {
            "active": 0,
            "paused": 0,
            "handoff_ready": 0,
            "total": 0,
        }
        if server.session_manager is not None:
            try:
                # Use count_by_status for efficient grouped counts
                status_counts = await server.run_db(server.session_manager.count_by_status)
                session_stats["total"] = sum(status_counts.values())
                session_stats["active"] = status_counts.get("active", 0)
                session_stats["paused"] = status_counts.get("paused", 0)
                session_stats["handoff_ready"] = status_counts.get("handoff_ready", 0)
            except Exception as e:
                logger.warning("Failed to get session stats: %s", e)

        # Get task statistics using efficient count queries
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
        if server.task_manager is not None:
            try:

                def _collect_task_stats() -> dict[str, Any]:
                    # Use count_by_state for efficient grouped counts
                    state_counts = server.task_manager.count_by_state()
                    stats = dict(task_stats)
                    for key in (
                        "ready",
                        "in_progress",
                        "closed",
                        "needs_review",
                        "review_approved",
                        "escalated",
                    ):
                        stats[key] = state_counts.get(key, 0)
                    # Keep availability and recent closure counters alongside state buckets.
                    stats["ready_unblocked"] = server.task_manager.count_ready_tasks()
                    stats["blocked"] = server.task_manager.count_blocked_tasks()
                    stats["closed_24h"] = server.task_manager.count_closed_since(hours=24)
                    return stats

                task_stats = await server.run_db(_collect_task_stats)
            except Exception as e:
                logger.warning("Failed to get task stats: %s", e)

        # Get memory statistics
        memory_stats: dict[str, Any] = {"count": 0, "by_type": {}, "recent_count": 0}
        if server.memory_manager is not None:
            try:
                stats = await server.memory_manager.get_stats()
                memory_stats["count"] = stats.get("total_count", 0)
                memory_stats["by_type"] = stats.get("by_type", {})
                memory_stats["recent_count"] = stats.get("recent_count", 0)
            except Exception as e:
                logger.warning("Failed to get memory stats: %s", e)

            # Qdrant vector store status
            try:
                vector_store = getattr(server.memory_manager, "_vector_store", None)
                qdrant_url = _get_qdrant_url(server, vector_store)
                qdrant_configured = vector_store is not None or qdrant_url is not None
                qdrant_healthy = False
                if qdrant_url:
                    qdrant_healthy = await is_qdrant_healthy(qdrant_url)
                elif vector_store is not None:
                    qdrant_client = getattr(vector_store, "_client", None)
                    if qdrant_client is not None:
                        try:
                            await asyncio.to_thread(
                                qdrant_client.count, vector_store._collection_name
                            )
                            qdrant_healthy = True
                        except Exception as e:
                            logger.debug(
                                "Qdrant health check failed: %s: %s",
                                type(e).__name__,
                                e,
                                exc_info=True,
                            )
                            qdrant_healthy = False
                qdrant_status: dict[str, Any] = {
                    "configured": qdrant_configured,
                    "healthy": qdrant_healthy,
                }
                if vector_store is not None:
                    status_snapshot = getattr(vector_store, "status_snapshot", None)
                    if callable(status_snapshot):
                        snapshot = status_snapshot()
                        if isinstance(snapshot, dict):
                            qdrant_status.update(snapshot)
                memory_stats["qdrant"] = qdrant_status
            except Exception as e:
                logger.warning(
                    "Failed to check Qdrant status: %s: %s",
                    type(e).__name__,
                    e,
                )
                memory_stats["qdrant"] = {"configured": False, "healthy": False}

        memory_stats["falkordb"] = await _get_falkordb_memory_status(server)

        # Get pipeline execution statistics
        pipeline_stats: dict[str, Any] = {
            "running": 0,
            "waiting_approval": 0,
            "completed": 0,
            "failed": 0,
            "total": 0,
        }
        try:
            from gobby.storage.pipelines import LocalPipelineExecutionManager

            def _collect_pipeline_stats() -> dict[str, Any]:
                mgr = LocalPipelineExecutionManager(db=server.services.database, project_id=None)
                status_counts = mgr.count_by_status()
                stats = dict(pipeline_stats)
                for key in ["running", "waiting_approval", "completed", "failed"]:
                    stats[key] = status_counts.get(key, 0)
                stats["total"] = sum(
                    stats[k] for k in ["running", "waiting_approval", "completed", "failed"]
                )
                return stats

            pipeline_stats = await server.run_db(_collect_pipeline_stats)
        except Exception as e:
            logger.warning("Failed to get pipeline stats: %s", e)

        # Get skills statistics
        skills_stats: dict[str, Any] = {"total": 0}
        if server.skill_manager is not None:
            try:
                skills_stats["total"] = await server.run_db(server.skill_manager.count_skills)
            except Exception as e:
                logger.warning("Failed to get skills stats: %s", e)

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

        # Agent run statistics
        agent_stats: dict[str, int] = {"running": 0}
        try:
            from gobby.storage.agents import LocalAgentRunManager

            def _list_running_agents() -> list[Any]:
                arm = LocalAgentRunManager(server.services.database)
                return arm.list_running()

            runs = await server.run_db(_list_running_agents)
            agent_stats["running"] = len(runs)
        except Exception:
            logger.debug("Could not collect running agent count", exc_info=True)

        # Calculate response time
        response_time_ms = (time.perf_counter() - start_time) * 1000

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

        postgres_status = await _get_postgres_dashboard_status(server, database_status)
        postgres_code_index_healthy = True
        if postgres_status is not None:
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

        payload: dict[str, Any] = {
            "status": (
                "healthy"
                if (
                    server._running
                    and not hook_runtime.is_degraded
                    and not degraded_services
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
        }
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
