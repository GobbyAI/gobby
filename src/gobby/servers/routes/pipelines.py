"""
Pipeline routes for Gobby HTTP server.

Provides endpoints for running, approving, and monitoring pipelines.
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
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


def _batch_load_cron_info(database: Any, execution_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Load cron trigger info for a batch of pipeline execution IDs.

    Returns a dict mapping execution_id to {name, cron_job_id, cron_expr}.
    """
    if not execution_ids:
        return {}
    try:
        placeholders = ", ".join("?" for _ in execution_ids)
        rows = database.fetchall(
            f"""
            SELECT cr.pipeline_execution_id, cj.id as cron_job_id, cj.name, cj.cron_expr
            FROM cron_runs cr
            JOIN cron_jobs cj ON cr.cron_job_id = cj.id
            WHERE cr.pipeline_execution_id IN ({placeholders})
            """,  # nosec B608
            tuple(execution_ids),
        )
        return {
            row["pipeline_execution_id"]: {
                "name": row["name"],
                "cron_job_id": row["cron_job_id"],
                "cron_expr": row["cron_expr"],
            }
            for row in rows
        }
    except Exception:
        logger.warning("Failed to load cron info for executions", exc_info=True)
        return {}


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
        session_id: str | None = None,
        parent_execution_id: str | None = None,
        limit: int = Query(50, gt=0, le=200, description="Maximum results per page"),
        offset: int = Query(0, ge=0, description="Number of leading rows to skip"),
    ) -> dict[str, Any]:
        """
        List pipeline executions with optional filters and offset pagination.

        Returns:
            200: Page of executions with filter-scoped total + status_summary
            422: Invalid pagination (limit out of range, negative offset)
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
            session_id=session_id,
            parent_execution_id=parent_execution_id,
            limit=limit,
            offset=offset,
        )
        total, status_summary = execution_manager.execution_metrics(
            status=status_filter,
            pipeline_name=pipeline_name,
            session_id=session_id,
            parent_execution_id=parent_execution_id,
        )

        # Batch-load steps for all executions in one query
        all_steps = execution_manager.get_steps_for_executions([e.id for e in executions])

        # Batch-load cron info for executions
        cron_info = _batch_load_cron_info(server.services.database, [e.id for e in executions])

        result = []
        for execution in executions:
            steps = all_steps.get(execution.id, [])
            entry: dict[str, Any] = {
                "id": execution.id,
                "pipeline_name": execution.pipeline_name,
                "project_id": execution.project_id,
                "status": execution.status.value,
                "created_at": execution.created_at,
                "updated_at": execution.updated_at,
                "completed_at": execution.completed_at,
                "inputs_json": execution.inputs_json,
                "outputs_json": execution.outputs_json,
                "definition_json": execution.definition_json,
                "parent_execution_id": execution.parent_execution_id,
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
            cron = cron_info.get(execution.id)
            if cron:
                entry["cron_job_name"] = cron["name"]
                entry["cron_job_id"] = cron["cron_job_id"]
                entry["cron_expr"] = cron["cron_expr"]
            result.append(entry)

        return {
            "executions": result,
            "total": total,
            "limit": limit,
            "offset": offset,
            "status_summary": status_summary,
        }

    @router.get("/executions/search")
    async def search_executions(
        q: str,
        status: str | None = None,
        search_errors: bool = True,
        search_outputs: bool = False,
        project_id: str | None = None,
        limit: int = Query(20, gt=0, le=200, description="Maximum results per page"),
        offset: int = Query(0, ge=0, description="Number of leading rows to skip"),
    ) -> dict[str, Any]:
        """
        Search pipeline executions by text across pipeline names and step errors.

        Returns:
            200: Page of matching executions with filter-scoped total
            400: Missing query
            422: Invalid pagination (limit out of range, negative offset)
        """
        from gobby.storage.pipelines import LocalPipelineExecutionManager
        from gobby.workflows.pipeline_state import ExecutionStatus

        if not q or not q.strip():
            raise HTTPException(status_code=400, detail="Query parameter 'q' is required")

        execution_manager = LocalPipelineExecutionManager(
            db=server.services.database, project_id=project_id or ""
        )

        status_filter = None
        if status:
            try:
                status_filter = ExecutionStatus(status)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}") from None

        executions = execution_manager.search_executions(
            query=q.strip(),
            search_errors=search_errors,
            search_outputs=search_outputs,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        total = execution_manager.count_search_executions(
            query=q.strip(),
            search_errors=search_errors,
            search_outputs=search_outputs,
            status=status_filter,
        )

        result = []
        for execution in executions:
            result.append(
                {
                    "id": execution.id,
                    "pipeline_name": execution.pipeline_name,
                    "project_id": execution.project_id,
                    "status": execution.status.value,
                    "created_at": execution.created_at,
                    "updated_at": execution.updated_at,
                    "completed_at": execution.completed_at,
                }
            )

        return {
            "executions": result,
            "total": total,
            "limit": limit,
            "offset": offset,
            "query": q.strip(),
        }

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

        # Load cron trigger info
        cron_info = _batch_load_cron_info(server.services.database, [execution.id])

        result: dict[str, Any] = {
            "id": execution.id,
            "pipeline_name": execution.pipeline_name,
            "project_id": execution.project_id,
            "status": execution.status.value,
            "created_at": execution.created_at,
            "updated_at": execution.updated_at,
            "completed_at": execution.completed_at,
            "inputs_json": execution.inputs_json,
            "outputs_json": execution.outputs_json,
            "definition_json": execution.definition_json,
            "parent_execution_id": execution.parent_execution_id,
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
        cron = cron_info.get(execution.id)
        if cron:
            result["cron_job_name"] = cron["name"]
            result["cron_job_id"] = cron["cron_job_id"]
            result["cron_expr"] = cron["cron_expr"]

        return result

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
