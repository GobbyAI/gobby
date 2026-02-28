"""
Admin routes for Gobby HTTP server.

Provides status, metrics, config, and shutdown endpoints.
"""

import asyncio
import logging
import os
import re
import subprocess  # nosec B404 - subprocess needed for daemon restart
import sys
import time
from typing import TYPE_CHECKING, Any

import psutil
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from gobby.utils.metrics import Counter, get_metrics_collector
from gobby.utils.version import get_version

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# Map litellm model prefixes to Gobby provider names
_PROVIDER_PREFIX_MAP: dict[str, str] = {
    "haiku": "claude",
    "gemini": "gemini",
    "gpt": "codex",
    "o1": "codex",
    "o3": "codex",
    "o4": "codex",
}

# Exclude non-coding model categories
_EXCLUDED_KEYWORDS = (
    "audio",
    "image",
    "vision",
    "embedding",
    "realtime",
    "tts",
    "transcribe",
    "search",
    "robotics",
    "live",
    "nano",
    "customtools",
    "computer-use",
    "deep-research",
    "thinking",
    "exp",
)

# Minimum version filters — skip deprecated/retired generations
_MIN_VERSION_FILTERS: dict[str, re.Pattern[str]] = {
    # Gemini: skip 1.x and 2.0 (deprecated)
    "gemini": re.compile(r"^gemini-(1\.|2\.0)"),
    # GPT: skip 3.5, 4, 4o (retired for Codex)
    "codex": re.compile(r"^(gpt-(3\.5|4o|4(?!\.)|4-)|o1)"),
}


def _model_id_to_label(model_id: str) -> str:
    """Convert a model ID like 'gpt-5.3-codex' to 'GPT 5.3 Codex'."""
    # Split on hyphens, title-case each part
    parts = model_id.split("-")
    labelled: list[str] = []
    for part in parts:
        upper = part.upper()
        # Keep well-known acronyms uppercase
        if upper in ("GPT", "O1", "O3", "O4"):
            labelled.append(upper)
        else:
            labelled.append(part.capitalize())
    return " ".join(labelled)


def _discover_models() -> dict[str, list[dict[str, str]]]:
    """Discover models from LiteLLM's model_cost registry.

    Returns models grouped by Gobby provider name, each entry as
    ``{"value": "<model_id>", "label": "<Human Label>"}``.
    Excludes provider-scoped duplicates (containing ``/``), dated
    variants, numeric-suffix duplicates, non-coding categories, and
    very old model generations.
    """
    import litellm

    all_keys = list(litellm.model_cost.keys())

    # Only bare names (no / — those are provider-scoped duplicates like azure/gpt-5)
    bare = [m for m in all_keys if "/" not in m]

    # Exclude dated variants (-YYYYMMDD) and numeric-suffix duplicates (-NNN)
    bare = [m for m in bare if not re.search(r"-\d{6,}", m)]

    # Exclude "latest" aliases
    bare = [m for m in bare if "latest" not in m]

    # Exclude non-coding model categories
    bare = [m for m in bare if not any(kw in m for kw in _EXCLUDED_KEYWORDS)]

    groups: dict[str, list[dict[str, str]]] = {}
    for m in bare:
        # Determine provider
        provider: str | None = None
        for prefix, prov in _PROVIDER_PREFIX_MAP.items():
            if m.startswith(prefix):
                provider = prov
                break
        if provider is None:
            continue

        # Apply minimum-version filter
        version_filter = _MIN_VERSION_FILTERS.get(provider)
        if version_filter and version_filter.search(m):
            continue

        entry = {"value": m, "label": _model_id_to_label(m)}
        groups.setdefault(provider, []).append(entry)

    # Sort each group by value and prepend (default) entry
    result: dict[str, list[dict[str, str]]] = {}
    for provider, entries in sorted(groups.items()):
        sorted_entries = sorted(entries, key=lambda e: e["value"])
        result[provider] = [{"value": "", "label": "(default)"}, *sorted_entries]

    return result


def _fallback_models_from_config(server: "HTTPServer") -> dict[str, list[dict[str, str]]]:
    """Fall back to configured model lists when LiteLLM is unavailable."""
    result: dict[str, list[dict[str, str]]] = {}
    if server.services.config and server.services.config.llm_providers:
        llm_config = server.services.config.llm_providers
        for provider_name in ("claude", "codex", "gemini", "litellm"):
            provider_config = getattr(llm_config, provider_name, None)
            if provider_config:
                models = provider_config.get_models_list()
                if models:
                    entries = [{"value": "", "label": "(default)"}]
                    entries.extend({"value": m, "label": _model_id_to_label(m)} for m in models)
                    result[provider_name] = entries
    return result


def create_admin_router(server: "HTTPServer") -> APIRouter:
    """
    Create admin router with endpoints bound to server instance.

    Args:
        server: HTTPServer instance for accessing state and dependencies

    Returns:
        Configured APIRouter with admin endpoints
    """
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.get("/health")
    async def health_check() -> dict[str, str]:
        """Lightweight health check for startup probing. No I/O."""
        return {"status": "ok"}

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
        metrics = get_metrics_collector()
        background_tasks = {
            "active": len(server._background_tasks),
            "total": metrics._counters.get("background_tasks_total", Counter("", "")).value,
            "completed": metrics._counters.get(
                "background_tasks_completed_total", Counter("", "")
            ).value,
            "failed": metrics._counters.get("background_tasks_failed_total", Counter("", "")).value,
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
        task_stats = {"open": 0, "in_progress": 0, "closed": 0, "ready": 0, "blocked": 0}
        if server.task_manager is not None:
            try:
                # Use count_by_status for efficient grouped counts
                status_counts = server.task_manager.count_by_status()
                task_stats["open"] = status_counts.get("open", 0)
                task_stats["in_progress"] = status_counts.get("in_progress", 0)
                task_stats["closed"] = status_counts.get("closed", 0)
                # Get ready and blocked counts using dedicated count methods
                task_stats["ready"] = server.task_manager.count_ready_tasks()
                task_stats["blocked"] = server.task_manager.count_blocked_tasks()
            except Exception as e:
                logger.warning(f"Failed to get task stats: {e}")

        # Get memory statistics
        memory_stats: dict[str, Any] = {"count": 0}
        if server.memory_manager is not None:
            try:
                stats = server.memory_manager.get_stats()
                memory_stats["count"] = stats.get("total_count", 0)
            except Exception as e:
                logger.warning(f"Failed to get memory stats: {e}")

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

        # Get skills statistics
        skills_stats: dict[str, Any] = {"total": 0}
        if server._internal_manager:
            try:
                for registry in server._internal_manager.get_all_registries():
                    if registry.name == "gobby-skills":
                        result = await registry.call("list_skills", {"limit": 10000})
                        if result.get("success"):
                            skills_stats["total"] = result.get("count", 0)
                        break
            except Exception as e:
                logger.warning(f"Failed to get skills stats: {e}")

        # Compute total cached tools across downstream servers
        downstream_tools_count = 0
        if server.mcp_manager:
            for config in server.mcp_manager.server_configs:
                if config.tools:
                    downstream_tools_count += len(config.tools)

        # Calculate response time
        response_time_ms = (time.perf_counter() - start_time) * 1000

        return {
            "status": "healthy" if server._running else "degraded",
            "dev_mode": getattr(server.services, "dev_mode", False),
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
        metrics = get_metrics_collector()
        try:
            # Update daemon health metrics if available
            if server._daemon is not None:
                try:
                    uptime = server._daemon.uptime
                    if uptime is not None:
                        metrics.set_gauge("daemon_uptime_seconds", uptime)

                    # Get process info for daemon
                    process = psutil.Process(os.getpid())
                    memory_info = process.memory_info()
                    metrics.set_gauge("daemon_memory_usage_bytes", float(memory_info.rss))

                    cpu_percent = process.cpu_percent(interval=0)
                    metrics.set_gauge("daemon_cpu_percent", cpu_percent)
                except Exception as e:
                    logger.warning(f"Failed to update daemon metrics: {e}")

            # Update background task gauge
            metrics.set_gauge("background_tasks_active", float(len(server._background_tasks)))

            # Export in Prometheus format
            prometheus_output = metrics.export_prometheus()
            return PlainTextResponse(
                content=prometheus_output, media_type="text/plain; version=0.0.4"
            )

        except Exception as e:
            logger.error(f"Failed to export metrics: {e}", exc_info=True)
            raise

    @router.get("/models")
    async def get_models(provider: str | None = None) -> dict[str, Any]:
        """
        Get available LLM models discovered from LiteLLM's model registry.

        Query params:
            provider: Optional filter (e.g. "claude", "gpt", "gemini")

        Returns:
            Dictionary with models grouped by provider, default_model
        """
        # Determine default model from config or fallback
        default_model = "opus"
        if (
            server.services.config
            and server.services.config.llm_providers
            and server.services.config.llm_providers.default_model
        ):
            default_model = server.services.config.llm_providers.default_model

        # Discover models from LiteLLM registry
        try:
            models_by_provider = _discover_models()
        except Exception as e:
            logger.warning(f"LiteLLM discovery failed, falling back to config: {e}")
            # Fallback to config-based models
            models_by_provider = _fallback_models_from_config(server)

        # Apply provider filter
        if provider:
            filtered = {k: v for k, v in models_by_provider.items() if k == provider}
            models_by_provider = filtered

        return {
            "models": models_by_provider,
            "default_model": default_model,
        }

    @router.get("/config")
    async def get_config() -> dict[str, Any]:
        """
        Get daemon configuration and version information.

        Returns:
            Configuration data including ports, features, and versions
        """
        start_time = time.perf_counter()
        metrics = get_metrics_collector()
        metrics.inc_counter("http_requests_total")

        try:
            config_data = {
                "server": {
                    "port": server.port,
                    "test_mode": server.test_mode,
                    "running": server._running,
                    "version": get_version(),
                },
                "features": {
                    "session_manager": server.session_manager is not None,
                    "mcp_manager": server.mcp_manager is not None,
                },
                "endpoints": {
                    "mcp": [
                        "/api/mcp/{server_name}/tools/{tool_name}",
                    ],
                    "sessions": [
                        "/api/sessions/register",
                        "/api/sessions/{id}",
                    ],
                    "admin": [
                        "/api/admin/status",
                        "/api/admin/metrics",
                        "/api/admin/config",
                        "/api/admin/shutdown",
                    ],
                },
            }

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "config": config_data,
                "response_time_ms": response_time_ms,
            }

        except Exception as e:
            logger.error(f"Config retrieval error: {e}", exc_info=True)
            from fastapi import HTTPException

            raise HTTPException(status_code=500, detail=str(e)) from e

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
                "message": "Shutdown failed to initiate",
            }

    _restart_in_progress = False

    @router.post("/restart")
    async def restart() -> dict[str, Any]:
        """
        Graceful daemon restart endpoint.

        Spawns a detached restarter subprocess that waits for the current
        daemon to exit, then starts a new one. Returns immediately.
        """
        nonlocal _restart_in_progress

        start_time = time.perf_counter()
        metrics = get_metrics_collector()
        metrics.inc_counter("http_requests_total")

        if _restart_in_progress:
            return {"status": "already_restarting", "message": "Restart already in progress"}

        try:
            _restart_in_progress = True
            logger.info("Restart requested via HTTP endpoint")

            current_pid = os.getpid()
            port = server.port

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
with open(os.path.join(log_dir, "gobby-client.log"), "a") as log_file, \
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
            _restart_in_progress = False
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
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error reloading workflows: {e}", exc_info=True)
            from fastapi import HTTPException

            raise HTTPException(status_code=500, detail=str(e)) from e

    # --- Test endpoints (for E2E testing) ---

    class TestProjectRegisterRequest(BaseModel):
        """Request model for registering a test project."""

        project_id: str
        name: str
        repo_path: str | None = None

    @router.post("/test/register-project")
    async def register_test_project(request: TestProjectRegisterRequest) -> dict[str, Any]:
        """
        Register a test project in the database.

        This endpoint is for E2E testing. It ensures the project exists
        in the projects table so sessions can be created with valid project_ids.

        Args:
            request: Project registration details

        Returns:
            Registration confirmation
        """
        from fastapi import HTTPException

        from gobby.storage.projects import LocalProjectManager

        # Guard: Only available in test mode
        if not server.test_mode:
            raise HTTPException(
                status_code=403, detail="Test endpoints only available in test mode"
            )

        start_time = time.perf_counter()
        metrics = get_metrics_collector()
        metrics.inc_counter("http_requests_total")

        try:
            # Use server's session manager database to avoid creating separate connections
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            db = server.session_manager.db

            project_manager = LocalProjectManager(db)

            # Check if project exists
            existing = project_manager.get(request.project_id)
            if existing:
                return {
                    "status": "already_exists",
                    "project_id": existing.id,
                    "name": existing.name,
                }

            # Create the project with the specific ID
            from datetime import UTC, datetime

            now = datetime.now(UTC).isoformat()
            db.execute(
                """
                INSERT INTO projects (id, name, repo_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (request.project_id, request.name, request.repo_path, now, now),
            )

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "message": f"Registered test project {request.project_id}",
                "project_id": request.project_id,
                "name": request.name,
                "response_time_ms": response_time_ms,
            }

        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error registering test project: {e}", exc_info=True)
            from fastapi import HTTPException

            raise HTTPException(status_code=500, detail=str(e)) from e

    class TestAgentRegisterRequest(BaseModel):
        """Request model for registering a test agent."""

        run_id: str
        session_id: str
        parent_session_id: str
        mode: str = "terminal"

    @router.post("/test/register-agent")
    async def register_test_agent(request: TestAgentRegisterRequest) -> dict[str, Any]:
        """
        Register a test agent in the running agent registry.

        This endpoint is for E2E testing of inter-agent messaging.
        It allows tests to set up parent-child agent relationships
        without actually spawning agent processes.

        Args:
            request: Agent registration details

        Returns:
            Registration confirmation
        """
        from fastapi import HTTPException

        from gobby.agents.registry import RunningAgent, get_running_agent_registry

        # Guard: Only available in test mode
        if not server.test_mode:
            raise HTTPException(
                status_code=403, detail="Test endpoints only available in test mode"
            )

        start_time = time.perf_counter()
        metrics = get_metrics_collector()
        metrics.inc_counter("http_requests_total")

        try:
            registry = get_running_agent_registry()

            # Create and register the agent
            running_agent = RunningAgent(
                run_id=request.run_id,
                session_id=request.session_id,
                parent_session_id=request.parent_session_id,
                mode=request.mode,
            )
            registry.add(running_agent)

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "message": f"Registered test agent {request.run_id}",
                "agent": running_agent.to_dict(),
                "response_time_ms": response_time_ms,
            }

        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error registering test agent: {e}", exc_info=True)
            from fastapi import HTTPException

            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/test/unregister-agent/{run_id}")
    async def unregister_test_agent(run_id: str) -> dict[str, Any]:
        """
        Unregister a test agent from the running agent registry.

        Args:
            run_id: The agent run ID to remove

        Returns:
            Unregistration confirmation
        """
        from fastapi import HTTPException

        from gobby.agents.registry import get_running_agent_registry

        # Guard: Only available in test mode
        if not server.test_mode:
            raise HTTPException(
                status_code=403, detail="Test endpoints only available in test mode"
            )

        start_time = time.perf_counter()
        metrics = get_metrics_collector()
        metrics.inc_counter("http_requests_total")

        try:
            registry = get_running_agent_registry()
            agent = registry.remove(run_id)

            response_time_ms = (time.perf_counter() - start_time) * 1000

            if agent:
                return {
                    "status": "success",
                    "message": f"Unregistered test agent {run_id}",
                    "response_time_ms": response_time_ms,
                }
            else:
                return {
                    "status": "not_found",
                    "message": f"Agent {run_id} not found in registry",
                    "response_time_ms": response_time_ms,
                }

        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error unregistering test agent: {e}", exc_info=True)
            from fastapi import HTTPException

            raise HTTPException(status_code=500, detail=str(e)) from e

    class TestSessionUsageRequest(BaseModel):
        """Request body for setting test session usage."""

        session_id: str
        input_tokens: int = 0
        output_tokens: int = 0
        cache_creation_tokens: int = 0
        cache_read_tokens: int = 0
        total_cost_usd: float = 0.0

    @router.post("/test/set-session-usage")
    async def set_test_session_usage(request: TestSessionUsageRequest) -> dict[str, Any]:
        """
        Set usage statistics for a test session.

        This endpoint is for E2E testing of token budget throttling.
        It allows tests to set session usage values to simulate
        budget consumption.

        Args:
            request: Session usage details

        Returns:
            Update confirmation
        """
        from fastapi import HTTPException

        # Guard: Only available in test mode
        if not server.test_mode:
            raise HTTPException(
                status_code=403, detail="Test endpoints only available in test mode"
            )

        start_time = time.perf_counter()
        metrics = get_metrics_collector()
        metrics.inc_counter("http_requests_total")

        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            success = server.session_manager.update_usage(
                session_id=request.session_id,
                input_tokens=request.input_tokens,
                output_tokens=request.output_tokens,
                cache_creation_tokens=request.cache_creation_tokens,
                cache_read_tokens=request.cache_read_tokens,
                total_cost_usd=request.total_cost_usd,
            )

            response_time_ms = (time.perf_counter() - start_time) * 1000

            if success:
                return {
                    "status": "success",
                    "session_id": request.session_id,
                    "usage_set": {
                        "input_tokens": request.input_tokens,
                        "output_tokens": request.output_tokens,
                        "cache_creation_tokens": request.cache_creation_tokens,
                        "cache_read_tokens": request.cache_read_tokens,
                        "total_cost_usd": request.total_cost_usd,
                    },
                    "response_time_ms": response_time_ms,
                }
            else:
                return {
                    "status": "not_found",
                    "message": f"Session {request.session_id} not found",
                    "response_time_ms": response_time_ms,
                }

        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error setting test session usage: {e}", exc_info=True)
            from fastapi import HTTPException

            raise HTTPException(status_code=500, detail=str(e)) from e

    # ------------------------------------------------------------------
    # Setup state endpoints (for web UI onboarding handoff)
    # ------------------------------------------------------------------

    @router.get("/setup-state")
    async def get_setup_state() -> dict[str, Any]:
        """Return the setup wizard state from ``~/.gobby/setup_state.json``."""
        import json
        from pathlib import Path

        state_path = Path("~/.gobby/setup_state.json").expanduser()
        if not state_path.exists():
            return {"exists": False}
        try:
            data: dict[str, Any] = json.loads(state_path.read_text())
            data["exists"] = True
            return data
        except (json.JSONDecodeError, OSError) as exc:
            return {"exists": False, "error": str(exc)}

    class SetupStateUpdate(BaseModel):
        web_onboarding_complete: bool = False

    @router.post("/setup-state")
    async def update_setup_state(request: SetupStateUpdate) -> dict[str, Any]:
        """Allow the web UI to mark web onboarding as complete."""
        import json
        from pathlib import Path

        state_path = Path("~/.gobby/setup_state.json").expanduser()
        if not state_path.exists():
            return {"success": False, "error": "No setup state found"}
        try:
            data = json.loads(state_path.read_text())
            if request.web_onboarding_complete:
                data["web_onboarding_complete"] = True
            state_path.write_text(json.dumps(data, indent=2))
            return {"success": True}
        except (json.JSONDecodeError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    return router
