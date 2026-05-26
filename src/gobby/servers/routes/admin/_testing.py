"""Test endpoints for admin router (E2E simulation)."""

import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
            logger.error(f"Error registering test project: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    class TestAgentRegisterRequest(BaseModel):
        """Request model for registering a test agent."""

        run_id: str
        session_id: str
        parent_session_id: str
        agent_name: str | None = None
        status: str = "running"

    @router.post("/test/register-agent")
    async def register_test_agent(request: TestAgentRegisterRequest) -> dict[str, Any]:
        """
        Register a test agent run in the database.

        This endpoint is for E2E testing of inter-agent messaging.
        It allows tests to set up parent-child agent relationships
        without actually spawning agent processes.

        Args:
            request: Agent registration details

        Returns:
            Registration confirmation
        """
        from gobby.storage.agents import LocalAgentRunManager

        # Guard: Only available in test mode
        if not server.test_mode:
            raise HTTPException(
                status_code=403, detail="Test endpoints only available in test mode"
            )

        start_time = time.perf_counter()

        try:
            db = server.services.database
            if request.status not in {
                "pending",
                "running",
                "success",
                "cancelled",
                "error",
                "timeout",
            }:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "status must be one of: pending, running, success, "
                        "cancelled, error, timeout"
                    ),
                )

            arm = LocalAgentRunManager(db)

            # Create agent run in DB
            arm.create(
                run_id=request.run_id,
                parent_session_id=request.parent_session_id,
                provider="test",
                prompt="test agent",
                agent_name=request.agent_name,
            )
            if request.status != "pending":
                arm.start(request.run_id)
            arm.update_child_session(request.run_id, request.session_id)
            if request.status == "success":
                arm.complete(request.run_id, result="test agent completed")
            elif request.status == "cancelled":
                arm.cancel(request.run_id)
            elif request.status == "error":
                arm.fail(request.run_id, error="test agent failed")
            elif request.status == "timeout":
                arm.timeout(request.run_id)

            run = arm.get(request.run_id)
            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "message": f"Registered test agent {request.run_id}",
                "agent": run.to_dict() if run else {"run_id": request.run_id},
                "response_time_ms": response_time_ms,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error registering test agent: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/test/unregister-agent/{run_id}")
    async def unregister_test_agent(run_id: str) -> dict[str, Any]:
        """
        Unregister a test agent from the database.

        Args:
            run_id: The agent run ID to remove

        Returns:
            Unregistration confirmation
        """
        from gobby.storage.agents import LocalAgentRunManager

        # Guard: Only available in test mode
        if not server.test_mode:
            raise HTTPException(
                status_code=403, detail="Test endpoints only available in test mode"
            )

        start_time = time.perf_counter()

        try:
            db = server.services.database
            arm = LocalAgentRunManager(db)
            run = arm.get(run_id)

            response_time_ms = (time.perf_counter() - start_time) * 1000

            if run:
                arm.fail(run_id, error="Unregistered via test endpoint")
                return {
                    "status": "success",
                    "message": f"Unregistered test agent {run_id}",
                    "response_time_ms": response_time_ms,
                }
            else:
                return {
                    "status": "not_found",
                    "message": f"Agent {run_id} not found in database",
                    "response_time_ms": response_time_ms,
                }

        except Exception as e:
            logger.error(f"Error unregistering test agent: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    class TestSessionUsageRequest(BaseModel):
        """Request body for setting test session usage."""

        session_id: str
        input_tokens: int = 0
        output_tokens: int = 0
        cache_creation_tokens: int = 0
        cache_read_tokens: int = 0

    @router.post("/test/set-session-usage")
    async def set_test_session_usage(request: TestSessionUsageRequest) -> dict[str, Any]:
        """
        Set usage statistics for a test session.

        This endpoint is for E2E testing of usage reporting.
        It allows tests to set session usage values directly.

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

        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            success = server.session_manager.update_usage(
                session_id=request.session_id,
                input_tokens=request.input_tokens,
                output_tokens=request.output_tokens,
                cache_creation_tokens=request.cache_creation_tokens,
                cache_read_tokens=request.cache_read_tokens,
            )

            if success:
                from datetime import UTC, datetime

                from gobby.storage.token_events import (
                    TokenEvent,
                    TokenEventStore,
                    canonicalize_event_timestamp,
                )

                try:
                    session_row = server.session_manager.get(request.session_id)
                    if session_row is not None:
                        token_event_store = TokenEventStore(server.session_manager.db)
                        token_event_store.record(
                            TokenEvent(
                                session_id=request.session_id,
                                project_id=getattr(session_row, "project_id", None),
                                message_id=None,
                                source=getattr(session_row, "source", None) or "test",
                                origin="test",
                                model=None,
                                input_tokens=request.input_tokens,
                                output_tokens=request.output_tokens,
                                cache_creation_tokens=request.cache_creation_tokens,
                                cache_read_tokens=request.cache_read_tokens,
                                event_at=canonicalize_event_timestamp(datetime.now(UTC)),
                            )
                        )
                except Exception as event_err:
                    logger.warning(
                        f"Failed to record token_event for test session {request.session_id}: {event_err}"
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
            logger.error(f"Error setting test session usage: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
