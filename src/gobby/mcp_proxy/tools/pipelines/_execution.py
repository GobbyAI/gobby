"""Pipeline execution tools."""

import asyncio
import json
import logging
from typing import Any, Protocol

from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline_state import (
    ApprovalRequired,
    ExecutionStatus,
    PipelineExecution,
    StepExecution,
    StepStatus,
)

logger = logging.getLogger(__name__)

# Track background pipeline tasks so they can be awaited on shutdown
_background_tasks: set[asyncio.Task[None]] = set()


async def cleanup_background_tasks() -> None:
    """Cancel and await all background pipeline tasks.

    Called during daemon shutdown to ensure no fire-and-forget tasks
    are left dangling.
    """
    if not _background_tasks:
        return

    tasks = list(_background_tasks)
    logger.info(f"Cancelling {len(tasks)} background pipeline task(s)")

    for task in tasks:
        task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            logger.warning(f"Pipeline task {task.get_name()} raised during shutdown: {result}")

    _background_tasks.clear()


class PipelineLoader(Protocol):
    async def load_pipeline(self, name: str) -> PipelineDefinition | None: ...


class PipelineExecutionManager(Protocol):
    def get_execution(self, execution_id: str) -> PipelineExecution | None: ...
    def get_steps_for_execution(self, execution_id: str) -> list[StepExecution]: ...
    def update_execution_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        resume_token: str | None = None,
        outputs_json: str | None = None,
    ) -> PipelineExecution | None: ...
    def update_step_execution(
        self,
        step_execution_id: int,
        status: StepStatus | None = None,
        output_json: str | None = None,
        error: str | None = None,
        approval_token: str | None = None,
        approved_by: str | None = None,
        approval_timeout_seconds: int | None = None,
    ) -> StepExecution | None: ...
    def create_execution(
        self, pipeline_name: str, inputs_json: str, session_id: str | None = None
    ) -> PipelineExecution: ...
    def list_executions(self, status: ExecutionStatus) -> list[PipelineExecution]: ...


class PipelineExecutor(Protocol):
    @property
    def execution_manager(self) -> PipelineExecutionManager: ...

    async def execute(
        self,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        project_id: str,
        execution_id: str | None = None,
        session_id: str | None = None,
    ) -> PipelineExecution: ...
    async def approve(self, token: str, approved_by: str | None = None) -> PipelineExecution: ...
    async def reject(self, token: str, rejected_by: str | None = None) -> PipelineExecution: ...


def _register_background_task(task: asyncio.Task[None]) -> None:
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task[None]) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception():
            logger.error(f"Pipeline background task failed: {t.exception()}")

    task.add_done_callback(_on_done)


async def _execute_pipeline_background(
    executor: PipelineExecutor,
    pipeline: PipelineDefinition,
    inputs: dict[str, Any],
    project_id: str,
    execution_id: str,
    pipeline_name: str,
    session_id: str | None = None,
) -> None:
    """Background task that runs a pre-created pipeline execution to completion."""
    try:
        await executor.execute(
            pipeline=pipeline,
            inputs=inputs,
            project_id=project_id,
            execution_id=execution_id,
            session_id=session_id,
        )
    except ApprovalRequired:
        # Expected — pipeline paused for approval, not an error
        pass
    except Exception as e:
        logger.error(f"Background pipeline '{pipeline_name}' failed: {e}", exc_info=True)
        # Ensure execution is marked failed even if executor.execute didn't catch it
        try:
            from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

            # Fallback: fix any steps stuck at 'running' status
            try:
                steps = executor.execution_manager.get_steps_for_execution(execution_id)
                for step in steps:
                    if step.status == StepStatus.RUNNING:
                        executor.execution_manager.update_step_execution(
                            step_execution_id=step.id,
                            status=StepStatus.FAILED,
                            error=str(e),
                        )
            except Exception:
                logger.error("Failed to clean up stuck steps", exc_info=True)

            executor.execution_manager.update_execution_status(
                execution_id=execution_id,
                status=ExecutionStatus.FAILED,
                outputs_json=json.dumps({"error": str(e)}),
            )
        except Exception:
            logger.error("Failed to mark execution as failed", exc_info=True)


async def run_pipeline(
    loader: PipelineLoader | None,
    executor: Any | None,
    name: str,
    inputs: dict[str, Any],
    project_id: str,
    session_id: str | None = None,
    wait: bool = False,
    wait_timeout: int = 300,
) -> dict[str, Any]:
    """
    Run a pipeline by name.

    When wait=False (default), the pipeline is launched as a background task
    and the execution_id is returned immediately for polling via
    get_pipeline_status.

    When wait=True, the pipeline is awaited directly and the full execution
    status (with all step outputs) is returned upon completion. If the
    pipeline does not finish within wait_timeout seconds, partial results
    are returned and the pipeline continues running in the background.

    Args:
        loader: WorkflowLoader instance
        executor: PipelineExecutor instance
        name: Pipeline name to run
        inputs: Input values for the pipeline
        project_id: Project context for the execution
        session_id: Optional session that triggered execution
        wait: If True, block until pipeline completes (default: False)
        wait_timeout: Max seconds to wait when wait=True (default: 300)

    Returns:
        Dict with execution_id and status/results
    """
    if not executor:
        return {"success": False, "error": "No executor configured"}

    if not loader:
        return {"success": False, "error": "No loader configured"}

    # Load the pipeline definition
    try:
        pipeline = await loader.load_pipeline(name)
    except ValueError as e:
        return {"success": False, "error": f"Invalid pipeline '{name}': {e}"}

    if not pipeline:
        return {"success": False, "error": f"Pipeline '{name}' not found"}

    # Pre-create execution record so we can return the ID immediately
    try:
        execution = executor.execution_manager.create_execution(
            pipeline_name=name,
            inputs_json=json.dumps(inputs),
            session_id=session_id,
        )
        execution_id = execution.id
    except Exception as e:
        return {"success": False, "error": f"Failed to create execution record: {e}"}

    task = asyncio.create_task(
        _execute_pipeline_background(
            executor,
            pipeline,
            inputs,
            project_id,
            execution_id,
            name,
            session_id=session_id,
        ),
        name=f"pipeline-{name}-{execution_id[:8]}",
    )
    _register_background_task(task)

    if wait:
        # Block until completion or timeout — asyncio.wait does NOT cancel
        # the task on timeout, so the pipeline keeps running
        done, _ = await asyncio.wait({task}, timeout=wait_timeout)

        status = get_pipeline_status(executor.execution_manager, execution_id)
        if not done:
            status["timed_out"] = True
            status["message"] = (
                f"Pipeline still running after {wait_timeout}s. "
                "Use get_pipeline_status to poll for completion."
            )
        return status

    return {
        "success": True,
        "status": "running",
        "execution_id": execution_id,
        "message": f"Pipeline '{name}' started. Poll status with get_pipeline_status.",
    }


async def approve_pipeline(
    executor: PipelineExecutor,
    token: str,
    approved_by: str | None = None,
) -> dict[str, Any]:
    """
    Approve a pipeline execution waiting for approval.

    Args:
        executor: PipelineExecutor instance
        token: Approval token from the waiting execution
        approved_by: Identifier of who approved (email, user ID, etc.)

    Returns:
        Dict with execution status
    """
    if not executor:
        return {"success": False, "error": "No executor configured"}

    try:
        execution = await executor.approve(
            token=token,
            approved_by=approved_by,
        )

        return {
            "success": True,
            "status": execution.status.value,
            "execution_id": execution.id,
        }

    except ValueError as e:
        return {"success": False, "error": str(e)}

    except Exception as e:
        return {"success": False, "error": f"Approval failed: {e}"}


async def reject_pipeline(
    executor: PipelineExecutor,
    token: str,
    rejected_by: str | None = None,
) -> dict[str, Any]:
    """
    Reject a pipeline execution waiting for approval.

    Args:
        executor: PipelineExecutor instance
        token: Approval token from the waiting execution
        rejected_by: Identifier of who rejected (email, user ID, etc.)

    Returns:
        Dict with execution status (cancelled)
    """
    if not executor:
        return {"success": False, "error": "No executor configured"}

    try:
        execution = await executor.reject(
            token=token,
            rejected_by=rejected_by,
        )

        return {
            "success": True,
            "status": execution.status.value,
            "execution_id": execution.id,
        }

    except ValueError as e:
        return {"success": False, "error": str(e)}

    except Exception as e:
        return {"success": False, "error": f"Rejection failed: {e}"}


async def resume_interrupted_pipelines(
    loader: PipelineLoader,
    executor: PipelineExecutor,
    execution_manager: PipelineExecutionManager,
    project_id: str | None = None,
) -> list[str]:
    """Resume pipelines that were running when the daemon last stopped.

    Finds RUNNING executions whose pipeline definition has resume_on_restart=True,
    re-queues them as background tasks using the existing resume path (execution_id),
    and returns the list of resumed execution IDs. Non-resumable executions are left
    RUNNING so the caller can fail them via fail_stale_running_executions(exclude_ids=...).

    Args:
        loader: WorkflowLoader for loading pipeline definitions.
        executor: PipelineExecutor instance.
        execution_manager: LocalPipelineExecutionManager instance.
        project_id: Current project ID.

    Returns:
        List of execution IDs that were successfully re-queued.
    """
    from gobby.workflows.pipeline_state import ExecutionStatus

    running = execution_manager.list_executions(status=ExecutionStatus.RUNNING)
    if not running:
        return []

    resumed: list[str] = []
    for execution in running:
        try:
            pipeline = await loader.load_pipeline(execution.pipeline_name)
        except ValueError as e:
            logger.warning(
                f"Cannot load pipeline '{execution.pipeline_name}' for "
                f"execution {execution.id} — will be failed: {e}"
            )
            continue

        if not pipeline:
            continue

        if not getattr(pipeline, "resume_on_restart", False):
            continue

        # Parse stored inputs
        inputs: dict[str, Any] = {}
        if execution.inputs_json:
            try:
                inputs = json.loads(execution.inputs_json)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("Malformed inputs_json for execution %s: %s", execution.id, e)

        # Re-queue as background task with existing execution_id (resume path)
        task = asyncio.create_task(
            _execute_pipeline_background(
                executor,
                pipeline,
                inputs,
                project_id or "",
                execution.id,
                execution.pipeline_name,
                session_id=execution.session_id,
            ),
            name=f"pipeline-resume-{execution.pipeline_name}-{execution.id[:8]}",
        )
        _register_background_task(task)
        resumed.append(execution.id)
        logger.info(f"Resumed pipeline '{execution.pipeline_name}' execution {execution.id}")

    return resumed


def get_pipeline_status(
    execution_manager: PipelineExecutionManager,
    execution_id: str,
) -> dict[str, Any]:
    """
    Get the status of a pipeline execution.

    Args:
        execution_manager: LocalPipelineExecutionManager instance
        execution_id: Execution ID to query

    Returns:
        Dict with execution details and step statuses
    """
    if not execution_manager:
        return {"success": False, "error": "No execution manager configured"}

    try:
        execution = execution_manager.get_execution(execution_id)
        if not execution:
            return {"success": False, "error": f"Execution '{execution_id}' not found"}

        # Get step executions
        steps = execution_manager.get_steps_for_execution(execution_id)

        # Parse inputs if available
        inputs = None
        if execution.inputs_json:
            try:
                inputs = json.loads(execution.inputs_json)
            except json.JSONDecodeError:
                inputs = execution.inputs_json

        # Parse outputs if available
        outputs = None
        if execution.outputs_json:
            try:
                outputs = json.loads(execution.outputs_json)
            except json.JSONDecodeError:
                outputs = execution.outputs_json

        # Build execution dict
        execution_dict = {
            "id": execution.id,
            "pipeline_name": execution.pipeline_name,
            "project_id": execution.project_id,
            "status": execution.status.value,
            "inputs": inputs,
            "outputs": outputs,
            "created_at": execution.created_at,
            "updated_at": execution.updated_at,
            "completed_at": execution.completed_at,
            "resume_token": execution.resume_token,
            "session_id": execution.session_id,
        }

        # Build steps list
        steps_list = []
        for step in steps:
            step_output = None
            if step.output_json:
                try:
                    step_output = json.loads(step.output_json)
                except json.JSONDecodeError:
                    step_output = step.output_json

            steps_list.append(
                {
                    "id": step.id,
                    "step_id": step.step_id,
                    "status": step.status.value,
                    "started_at": step.started_at,
                    "completed_at": step.completed_at,
                    "output": step_output,
                    "error": step.error,
                    "approval_token": step.approval_token,
                    "approved_by": step.approved_by,
                    "approved_at": step.approved_at,
                }
            )

        return {
            "success": True,
            "execution": execution_dict,
            "steps": steps_list,
        }

    except Exception as e:
        return {"success": False, "error": f"Failed to get status: {e}"}
