"""Health, status, and metrics endpoints for admin router."""

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import psutil
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from gobby.telemetry.instruments import get_all_metrics, set_gauge, update_daemon_metrics

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def register_health_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.get("/health")
    async def health_check() -> dict[str, str]:
        """Lightweight health check for startup probing. No I/O."""
        return {"status": "ok"}

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
                logger.warning(f"Failed to get daemon status: {e}")

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
            logger.warning(f"Failed to get process metrics: {e}")
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
                    is_connected = config.name in server.mcp_manager.connections
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
                        "tool_count": len(config.tools) if config.tools else 0,
                    }
            except Exception as e:
                logger.warning(f"Failed to get MCP health: {e}")

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
                    "internal": True,  # Flag to distinguish from downstream servers
                    "tool_count": len(tools),
                }

        # Get session statistics using efficient count queries
        session_stats = {"active": 0, "paused": 0, "handoff_ready": 0, "total": 0}
        if server.session_manager is not None:
            try:
                # Use count_by_status for efficient grouped counts
                status_counts = server.session_manager.count_by_status()
                session_stats["total"] = sum(status_counts.values())
                session_stats["active"] = status_counts.get("active", 0)
                session_stats["paused"] = status_counts.get("paused", 0)
                session_stats["handoff_ready"] = status_counts.get("handoff_ready", 0)
            except Exception as e:
                logger.warning(f"Failed to get session stats: {e}")

        # Get task statistics using efficient count queries
        task_stats: dict[str, Any] = {
            "open": 0,
            "in_progress": 0,
            "closed": 0,
            "needs_review": 0,
            "review_approved": 0,
            "escalated": 0,
            "ready": 0,
            "blocked": 0,
            "closed_24h": 0,
        }
        if server.task_manager is not None:
            try:
                # Use count_by_status for efficient grouped counts
                status_counts = server.task_manager.count_by_status()
                for key in (
                    "open",
                    "in_progress",
                    "closed",
                    "needs_review",
                    "review_approved",
                    "escalated",
                ):
                    task_stats[key] = status_counts.get(key, 0)
                # Get ready, blocked, and recent closed counts
                task_stats["ready"] = server.task_manager.count_ready_tasks()
                task_stats["blocked"] = server.task_manager.count_blocked_tasks()
                task_stats["closed_24h"] = server.task_manager.count_closed_since(hours=24)
            except Exception as e:
                logger.warning(f"Failed to get task stats: {e}")

        # Get memory statistics
        memory_stats: dict[str, Any] = {"count": 0, "by_type": {}, "recent_count": 0}
        if server.memory_manager is not None:
            try:
                stats = server.memory_manager.get_stats()
                memory_stats["count"] = stats.get("total_count", 0)
                memory_stats["by_type"] = stats.get("by_type", {})
                memory_stats["recent_count"] = stats.get("recent_count", 0)
            except Exception as e:
                logger.warning(f"Failed to get memory stats: {e}")

            # Qdrant vector store status
            try:
                vector_store = getattr(server.memory_manager, "_vector_store", None)
                qdrant_configured = vector_store is not None
                qdrant_healthy = False
                if vector_store is not None:
                    qdrant_client = getattr(vector_store, "_client", None)
                    if qdrant_client is not None:
                        try:
                            await asyncio.to_thread(
                                qdrant_client.count, vector_store._collection_name
                            )
                            qdrant_healthy = True
                        except Exception:
                            logger.debug("Qdrant health check failed", exc_info=True)
                            qdrant_healthy = False
                memory_stats["qdrant"] = {
                    "configured": qdrant_configured,
                    "healthy": qdrant_healthy,
                }
            except Exception as e:
                logger.warning(f"Failed to check Qdrant status: {e}")
                memory_stats["qdrant"] = {"configured": False, "healthy": False}

            # Neo4j knowledge graph status
            try:
                from gobby.cli.services import is_neo4j_healthy, is_neo4j_installed

                neo4j_client = getattr(server.memory_manager, "_neo4j_client", None)
                neo4j_url = neo4j_client.base_url if neo4j_client else None
                installed = is_neo4j_installed()
                healthy = await is_neo4j_healthy(neo4j_url) if neo4j_url else False
                memory_stats["neo4j"] = {
                    "configured": neo4j_client is not None,
                    "installed": installed,
                    "healthy": healthy,
                    "url": neo4j_url,
                }
            except Exception as e:
                logger.warning(f"Failed to check Neo4j status: {e}")
                memory_stats["neo4j"] = {"configured": False, "installed": False, "healthy": False}

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

            mgr = LocalPipelineExecutionManager(db=server.services.database, project_id="")
            status_counts = mgr.count_by_status()
            for key in ["running", "waiting_approval", "completed", "failed"]:
                pipeline_stats[key] = status_counts.get(key, 0)
            pipeline_stats["total"] = sum(
                pipeline_stats[k] for k in ["running", "waiting_approval", "completed", "failed"]
            )
        except Exception as e:
            logger.warning(f"Failed to get pipeline stats: {e}")

        # Get skills statistics
        skills_stats: dict[str, Any] = {"total": 0}
        if server.skill_manager is not None:
            try:
                skills_stats["total"] = server.skill_manager.count_skills()
            except Exception as e:
                logger.warning(f"Failed to get skills stats: {e}")

        # Compute total cached tools across downstream servers
        downstream_tools_count = 0
        if server.mcp_manager:
            for config in server.mcp_manager.server_configs:
                if config.tools:
                    downstream_tools_count += len(config.tools)

        # Get savings summary
        savings_stats: dict[str, Any] = {
            "total_tokens_saved": 0,
            "total_events": 0,
            "categories": {},
        }
        try:
            from gobby.servers.routes.admin._savings import _get_tracker

            tracker = _get_tracker(server)
            if tracker:
                today = tracker.get_summary(days=1)
                cumulative = tracker.get_cumulative(days=30)
                savings_stats = {
                    "today_tokens_saved": today.get("total_tokens_saved", 0),
                    "today_events": today.get("total_events", 0),
                    "cumulative_tokens_saved": cumulative.get("total_tokens_saved", 0),
                    "categories": today.get("categories", {}),
                }
        except Exception as e:
            logger.warning(f"Failed to get savings stats: {e}")

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
            pass

        # Database size
        db_size_bytes: int | None = None
        try:
            from gobby.cli.utils import get_gobby_home as _ghome

            db_path = _ghome() / "gobby-hub.db"
            if db_path.exists():
                db_size_bytes = db_path.stat().st_size
        except Exception:
            pass

        # Last shutdown source
        last_shutdown: str | None = None
        try:
            import json as _json

            from gobby.cli.utils import get_gobby_home as _ghome2

            source_file = _ghome2() / "shutdown_source.json"
            if source_file.exists():
                data = _json.loads(source_file.read_text())
                last_shutdown = data.get("source", "unknown")
        except Exception:
            pass

        # Agent run statistics
        agent_stats: dict[str, int] = {"running": 0}
        try:
            from gobby.storage.agents import LocalAgentRunManager

            arm = LocalAgentRunManager(server.services.database)
            runs = arm.list_running()
            agent_stats["running"] = len(runs)
        except Exception:
            pass

        # Calculate response time
        response_time_ms = (time.perf_counter() - start_time) * 1000

        return {
            "status": "healthy" if server._running else "degraded",
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
            "savings": savings_stats,
            "agents": agent_stats,
            "fd_usage": fd_usage,
            "db_size_bytes": db_size_bytes,
            "last_shutdown": last_shutdown,
            "response_time_ms": response_time_ms,
        }

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
            logger.error(f"Failed to export metrics: {e}", exc_info=True)
            raise
