"""
Pipeline routes for Gobby HTTP server.

Provides endpoints for running, approving, and monitoring pipelines.
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class PipelineRunRequest(BaseModel):
    """Request body for POST /api/pipelines/run."""

    name: str
    inputs: dict[str, Any] = {}
    project_id: str | None = None


class PipelineRunResponse(BaseModel):
    """Response body for successful pipeline execution."""

    status: str
    execution_id: str
    pipeline_name: str
    outputs: dict[str, Any] | None = None


class PipelineApprovalResponse(BaseModel):
    """Response body when pipeline requires approval."""

    status: str
    execution_id: str
    step_id: str
    token: str
    message: str


def create_pipelines_router(server: "HTTPServer") -> APIRouter:
    """
    Create pipelines router with endpoints bound to server instance.

    Args:
        server: HTTPServer instance for accessing state and dependencies

    Returns:
        Configured APIRouter with pipeline endpoints
    """
    router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])

    @router.get("/executions")
    async def list_executions(
        status: str | None = None,
        pipeline_name: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        List pipeline executions with optional filters.

        Returns:
            200: List of executions with steps
        """
        from gobby.storage.pipelines import LocalPipelineExecutionManager
        from gobby.workflows.pipeline_state import ExecutionStatus

        execution_manager = LocalPipelineExecutionManager(
            db=server.services.database, project_id=project_id or ""
        )

        status_filter = None
        if status:
            try:
                status_filter = ExecutionStatus(status)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}") from None

        executions = execution_manager.list_executions(
            status=status_filter,
            pipeline_name=pipeline_name,
            limit=limit,
        )

        # Batch-load steps for all executions in one query
        all_steps = execution_manager.get_steps_for_executions([e.id for e in executions])

        result = []
        for execution in executions:
            steps = all_steps.get(execution.id, [])
            result.append(
                {
                    "id": execution.id,
                    "pipeline_name": execution.pipeline_name,
                    "project_id": execution.project_id,
                    "status": execution.status.value,
                    "created_at": execution.created_at,
                    "updated_at": execution.updated_at,
                    "completed_at": execution.completed_at,
                    "inputs_json": execution.inputs_json,
                    "outputs_json": execution.outputs_json,
                    "steps": [
                        {
                            "id": step.id,
                            "step_id": step.step_id,
                            "status": step.status.value,
                            "started_at": step.started_at,
                            "completed_at": step.completed_at,
                            "output_json": step.output_json,
                            "error": step.error,
                        }
                        for step in steps
                    ],
                }
            )

        return {"executions": result, "count": len(result)}

    @router.post("/run", response_model=None)
    async def run_pipeline(request: PipelineRunRequest) -> dict[str, Any] | JSONResponse:
        """
        Run a pipeline by name.

        Returns:
            200: Pipeline completed successfully
            202: Pipeline waiting for approval
            404: Pipeline not found
            500: Execution error
        """
        from gobby.workflows.pipeline_state import ApprovalRequired

        # Get loader from services; executor is resolved per-project
        loader = server.services.workflow_loader

        if loader is None:
            raise HTTPException(status_code=500, detail="Workflow loader not configured")

        project_id = request.project_id or ""
        if not project_id:
            raise HTTPException(
                status_code=400, detail="project_id required for pipeline execution"
            )

        executor = server.services.get_pipeline_executor(project_id)
        if executor is None:
            raise HTTPException(
                status_code=500, detail="Pipeline executor not available for project"
            )

        # Load the pipeline
        pipeline = await loader.load_pipeline(request.name)
        if pipeline is None:
            raise HTTPException(status_code=404, detail=f"Pipeline '{request.name}' not found")

        try:
            # Execute the pipeline
            execution = await executor.execute(
                pipeline=pipeline,
                inputs=request.inputs,
                project_id=request.project_id or "",
            )

            # Return success response
            return {
                "status": execution.status.value,
                "execution_id": execution.id,
                "pipeline_name": execution.pipeline_name,
            }

        except ApprovalRequired as e:
            # Return 202 Accepted for approval required
            return JSONResponse(
                status_code=202,
                content={
                    "status": "waiting_approval",
                    "execution_id": e.execution_id,
                    "step_id": e.step_id,
                    "token": e.token,
                    "message": e.message,
                },
            )

        except Exception as e:
            logger.error(f"Pipeline execution failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Execution error: {e}") from None

    @router.get("/{execution_id}")
    async def get_execution(execution_id: str) -> dict[str, Any]:
        """
        Get execution details by ID.

        Returns:
            200: Execution details with steps
            404: Execution not found
        """
        # Create lightweight execution manager for read-only queries
        from gobby.storage.pipelines import LocalPipelineExecutionManager

        execution_manager = LocalPipelineExecutionManager(
            db=server.services.database, project_id=""
        )

        # Fetch execution
        execution = execution_manager.get_execution(execution_id)
        if execution is None:
            raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")

        # Fetch steps
        steps = execution_manager.get_steps_for_execution(execution_id)

        return {
            "id": execution.id,
            "pipeline_name": execution.pipeline_name,
            "project_id": execution.project_id,
            "status": execution.status.value,
            "created_at": execution.created_at,
            "updated_at": execution.updated_at,
            "completed_at": execution.completed_at,
            "inputs_json": execution.inputs_json,
            "outputs_json": execution.outputs_json,
            "steps": [
                {
                    "id": step.id,
                    "step_id": step.step_id,
                    "status": step.status.value,
                    "started_at": step.started_at,
                    "completed_at": step.completed_at,
                    "output_json": step.output_json,
                    "error": step.error,
                    "approval_token": step.approval_token,
                }
                for step in steps
            ],
        }

    @router.post("/approve/{token}", response_model=None)
    async def approve_execution(token: str) -> dict[str, Any] | JSONResponse:
        """
        Approve a pipeline execution waiting for approval.

        Returns:
            200: Execution resumed and completed (or continued)
            202: Execution resumed but needs another approval
            404: Invalid token
        """
        from gobby.storage.pipelines import LocalPipelineExecutionManager
        from gobby.workflows.pipeline_state import ApprovalRequired

        # Look up the execution's project from the approval token
        global_mgr = LocalPipelineExecutionManager(db=server.services.database, project_id="")
        step = global_mgr.get_step_by_approval_token(token)
        if not step:
            raise HTTPException(status_code=404, detail="Invalid approval token")
        execution_record = global_mgr.get_execution(step.execution_id)
        if not execution_record:
            raise HTTPException(status_code=404, detail="Execution not found")

        executor = server.services.get_pipeline_executor(execution_record.project_id)
        if executor is None:
            raise HTTPException(
                status_code=500, detail="Pipeline executor not available for project"
            )

        try:
            execution = await executor.approve(token, approved_by=None)

            return {
                "status": execution.status.value,
                "execution_id": execution.id,
                "pipeline_name": execution.pipeline_name,
            }

        except ApprovalRequired as e:
            # Pipeline needs another approval
            return JSONResponse(
                status_code=202,
                content={
                    "status": "waiting_approval",
                    "execution_id": e.execution_id,
                    "step_id": e.step_id,
                    "token": e.token,
                    "message": e.message,
                },
            )

        except ValueError as e:
            raise HTTPException(status_code=404, detail=f"Invalid token: {e}") from None

    @router.post("/reject/{token}")
    async def reject_execution(token: str) -> dict[str, Any]:
        """
        Reject a pipeline execution waiting for approval.

        Returns:
            200: Execution rejected/cancelled
            404: Invalid token
        """
        from gobby.storage.pipelines import LocalPipelineExecutionManager

        # Look up the execution's project from the rejection token
        global_mgr = LocalPipelineExecutionManager(db=server.services.database, project_id="")
        step = global_mgr.get_step_by_approval_token(token)
        if not step:
            raise HTTPException(status_code=404, detail="Invalid rejection token")
        execution_record = global_mgr.get_execution(step.execution_id)
        if not execution_record:
            raise HTTPException(status_code=404, detail="Execution not found")

        executor = server.services.get_pipeline_executor(execution_record.project_id)
        if executor is None:
            raise HTTPException(
                status_code=500, detail="Pipeline executor not available for project"
            )

        try:
            execution = await executor.reject(token, rejected_by=None)

            return {
                "status": execution.status.value,
                "execution_id": execution.id,
                "pipeline_name": execution.pipeline_name,
            }

        except ValueError as e:
            raise HTTPException(status_code=404, detail=f"Invalid token: {e}") from None

    return router
