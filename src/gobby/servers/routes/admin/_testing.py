"""Test endpoints for admin router (E2E simulation)."""

import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gobby.utils.metrics import get_metrics_collector

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def register_testing_routes(router: APIRouter, server: "HTTPServer") -> None:
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
            logger.error("Error registering test project: %s", e, exc_info=True)
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
            logger.error("Error registering test agent: %s", e, exc_info=True)
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
            logger.error("Error unregistering test agent: %s", e, exc_info=True)
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
            logger.error("Error setting test session usage: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
