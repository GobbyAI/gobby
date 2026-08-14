"""Pipeline execution tools."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from gobby.storage.hub.protocol import HubDatabase
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
_background_tasks_by_execution: dict[str, asyncio.Task[None]] = {}

RunDb = Callable[..., Awaitable[Any]]


async def _run_sync_db(
    run_db: RunDb | None,
    operation: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    if run_db is not None:
        return await run_db(operation, *args, **kwargs)
    return await asyncio.to_thread(operation, *args, **kwargs)


async def cleanup_background_tasks() -> None:
    """Cancel and await all background pipeline tasks.

    Called during daemon shutdown to ensure no fire-and-forget tasks
    are left dangling.
    """
    if not _background_tasks:
        _background_tasks_by_execution.clear()
        return

    tasks = list(_background_tasks)
    logger.info("Cancelling %s background pipeline task(s)", len(tasks))

    for task in tasks:
        task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            logger.warning("Pipeline task %s raised during shutdown: %s", task.get_name(), result)

    _background_tasks.clear()
    _background_tasks_by_execution.clear()


class PipelineLoader(Protocol):
    async def load_pipeline(
        self,
        name: str,
        project_path: str | None = None,
    ) -> PipelineDefinition | None: ...


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

    def claim_failed_execution_for_resume(self, execution_id: str) -> PipelineExecution | None: ...
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
    def reset_steps_from(self, execution_id: str, from_step_id: str) -> int: ...
    def create_execution(
        self, pipeline_name: str, inputs_json: str, session_id: str | None = None
    ) -> PipelineExecution: ...
    def list_executions(
        self,
        status: ExecutionStatus | None = None,
        pipeline_name: str | None = None,
        session_id: str | None = None,
        parent_execution_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PipelineExecution]: ...


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


def _register_background_task(execution_id: str, task: asyncio.Task[None]) -> None:
    _background_tasks.add(task)
    _background_tasks_by_execution[execution_id] = task

    def _on_done(t: asyncio.Task[None]) -> None:
        _background_tasks.discard(t)
        if _background_tasks_by_execution.get(execution_id) is t:
            _background_tasks_by_execution.pop(execution_id, None)
        if not t.cancelled() and t.exception():
            logger.error("Pipeline background task failed: %s", t.exception())

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
        logger.exception("Background pipeline '%s' failed: %s", pipeline_name, e)
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
                logger.exception("Failed to clean up stuck steps")

            executor.execution_manager.update_execution_status(
                execution_id=execution_id,
                status=ExecutionStatus.FAILED,
                outputs_json=json.dumps({"error": str(e)}),
            )
        except Exception:
            logger.exception("Failed to mark execution as failed")


async def cancel_pipeline(
    execution_manager: PipelineExecutionManager | None,
    execution_id: str,
) -> dict[str, Any]:
    """
    Cancel a running pipeline execution.

    Marks the execution and any running steps as cancelled, and attempts
    to kill any spawned agents associated with this pipeline.

    Args:
        execution_manager: LocalPipelineExecutionManager instance
        execution_id: ID of the execution to cancel

    Returns:
        Dict with execution status (cancelled)
    """
    if not execution_manager:
        return {"success": False, "error": "No execution manager configured"}

    # Look up the execution
    execution = execution_manager.get_execution(execution_id)
    if not execution:
        return {"success": False, "error": f"Execution '{execution_id}' not found"}

    if execution.status in (
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    ):
        return {
            "success": False,
            "error": f"Pipeline already in a terminal state (current status: {execution.status.value})",
        }

    # Stop the exact fire-and-forget task before yielding to agent termination.
    background_task = _background_tasks_by_execution.get(execution_id)
    if (
        background_task
        and background_task is not asyncio.current_task()
        and not background_task.done()
    ):
        background_task.cancel()

    # Mark running steps as cancelled.
    try:
        steps = execution_manager.get_steps_for_execution(execution_id)
        for step in steps:
            if step.status in (StepStatus.RUNNING, StepStatus.PENDING):
                execution_manager.update_step_execution(
                    step_execution_id=step.id,
                    status=StepStatus.CANCELLED,
                )
    except Exception as e:
        logger.warning("Failed to cancel steps for pipeline %s: %s", execution_id, e)

    # Mark the execution as cancelled before the next await so the executor
    # cannot race cancellation and write COMPLETED.
    try:
        execution_manager.update_execution_status(
            execution_id=execution_id,
            status=ExecutionStatus.CANCELLED,
        )
    except Exception as e:
        return {"success": False, "error": f"Failed to update execution status: {e}"}

    if background_task and background_task is not asyncio.current_task():
        await asyncio.gather(background_task, return_exceptions=True)

    # Kill only agents owned by the deterministic pipeline child session.
    # Never fall back to execution.session_id because an early cancellation
    # may still contain the unrelated caller session there.
    try:
        from gobby.agents.kill import kill_agent
        from gobby.storage.agents import LocalAgentRunManager
        from gobby.storage.sessions import SessionManager

        _db: HubDatabase | None = getattr(execution_manager, "db", None)
        if _db is None:
            raise RuntimeError("execution manager has no database")
        pipeline_session = SessionManager(_db).find_active_by_external_id(
            f"pipeline-{execution_id}",
            "pipeline",
        )
        pipeline_session_id = pipeline_session.id if pipeline_session else None
        arm = LocalAgentRunManager(_db)
        active_runs = arm.list_by_parent(pipeline_session_id) if pipeline_session_id else []
        killed_count = 0
        for run in active_runs:
            await kill_agent(run, _db, signal_name="KILL")
            killed_count += 1

        if killed_count > 0:
            logger.info(
                "Killed %s agents associated with pipeline %s", killed_count, execution_id[:8]
            )
    except Exception as e:
        logger.warning("Failed to kill agents for pipeline %s: %s", execution_id, e)

    return {
        "success": True,
        "status": "cancelled",
        "execution_id": execution_id,
        "message": f"Pipeline '{execution.pipeline_name}' execution {execution_id[:8]} has been cancelled.",
    }


async def run_pipeline(
    loader: PipelineLoader | None,
    executor: Any | None,
    name: str,
    inputs: dict[str, Any],
    project_id: str,
    session_id: str | None = None,
    continuation_prompt: str | None = None,
) -> dict[str, Any]:
    """
    Run a pipeline by name.

    Always returns immediately with execution_id. The pipeline runs as a
    background task. Callers are notified via the completion event registry
    when the pipeline finishes.

    Args:
        loader: PipelineLoader instance
        executor: PipelineExecutor instance
        name: Pipeline name to run
        inputs: Input values for the pipeline
        project_id: Project context for the execution
        session_id: Optional session that triggered execution
        continuation_prompt: Instructions for what to do when the pipeline
            completes. Stored with the execution and included in the
            completion notification sent to subscribers.

    Returns:
        Dict with execution_id and status
    """
    if not executor:
        return {"success": False, "error": "No executor configured"}

    if not loader:
        return {"success": False, "error": "No loader configured"}

    # Load the pipeline definition
    try:
        pipeline = await loader.load_pipeline(name, project_id)
    except ValueError as e:
        return {"success": False, "error": f"Invalid pipeline '{name}': {e}"}

    if not pipeline:
        return {"success": False, "error": f"Pipeline '{name}' not found"}

    if not pipeline.enabled:
        return {"success": False, "error": f"Pipeline '{name}' is disabled"}

    # Pre-create execution record so we can return the ID immediately
    try:
        execution = executor.execution_manager.create_execution(
            pipeline_name=name,
            inputs_json=json.dumps(inputs),
            session_id=session_id,
            continuation_prompt=continuation_prompt,
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
    _register_background_task(execution_id, task)

    return {
        "success": True,
        "status": "running",
        "execution_id": execution_id,
        "message": (f"Pipeline '{name}' started. You will be notified when it completes."),
    }


async def resume_pipeline(
    loader: PipelineLoader | None,
    executor: Any | None,
    execution_manager: PipelineExecutionManager | None,
    execution_id: str,
    project_id: str,
    session_id: str | None = None,
    from_step: str | None = None,
) -> dict[str, Any]:
    """
    Resume a failed pipeline execution by resetting steps from the failure point.

    Determines the resume point (explicit from_step, or auto-detected first
    failed/errored step), resets that step and all subsequent steps to PENDING,
    then re-executes via the executor's resume path.

    Args:
        loader: PipelineLoader instance
        executor: PipelineExecutor instance
        execution_manager: LocalPipelineExecutionManager instance
        execution_id: ID of the failed execution to resume
        project_id: Project context for the execution
        session_id: Optional session that triggered the resume
        from_step: Optional step ID to resume from (resets this and all later steps)

    Returns:
        Dict with execution_id and status
    """
    if not executor:
        return {"success": False, "error": "No executor configured"}

    if not execution_manager:
        return {"success": False, "error": "No execution manager configured"}

    if not loader:
        return {"success": False, "error": "No loader configured"}

    # Look up the execution
    execution = execution_manager.get_execution(execution_id)
    if not execution:
        return {"success": False, "error": f"Execution '{execution_id}' not found"}

    if execution.status != ExecutionStatus.FAILED:
        return {
            "success": False,
            "error": f"Only failed pipelines can be resumed (current status: {execution.status.value})",
        }

    # Load the pipeline definition
    try:
        pipeline = await loader.load_pipeline(execution.pipeline_name, execution.project_id)
    except ValueError as e:
        return {"success": False, "error": f"Invalid pipeline '{execution.pipeline_name}': {e}"}

    if not pipeline:
        return {
            "success": False,
            "error": f"Pipeline '{execution.pipeline_name}' not found",
        }

    if not pipeline.enabled:
        return {
            "success": False,
            "error": f"Pipeline '{execution.pipeline_name}' is disabled",
        }

    # Determine resume point and reset steps
    steps = execution_manager.get_steps_for_execution(execution_id)
    if from_step:
        # User specified — validate it exists
        step_ids = [s.step_id for s in steps]
        if from_step not in step_ids:
            return {
                "success": False,
                "error": f"Step '{from_step}' not found. Available: {step_ids}",
            }
        resume_step_id = from_step
    else:
        # Auto-detect: first FAILED step, or first step with error data
        resume_step_id = None
        for step in steps:
            if step.status == StepStatus.FAILED:
                resume_step_id = step.step_id
                break
            if step.error and step.status in (StepStatus.COMPLETED, StepStatus.SKIPPED):
                logger.warning(
                    "Step %s has status %s but carries error: %s",
                    step.step_id,
                    step.status.value,
                    step.error[:200],
                )
                resume_step_id = step.step_id
                break
        if not resume_step_id:
            return {
                "success": False,
                "error": "No failed or errored step found to resume from",
            }

    if not resume_step_id:
        raise ValueError("resume_step_id resolved to None despite early-return guard")

    # Parse stored inputs
    inputs: dict[str, Any] = {}
    if execution.inputs_json:
        try:
            inputs = json.loads(execution.inputs_json)
        except (json.JSONDecodeError, TypeError) as e:
            return {
                "success": False,
                "error": f"Malformed inputs_json for execution {execution_id}: {e}",
            }

    # Atomically claim the failed execution. Both callers may have observed FAILED
    # above, but only one may transition it to RUNNING and mutate its steps.
    claimed = execution_manager.claim_failed_execution_for_resume(execution_id)
    if claimed is None:
        return {
            "success": False,
            "error": (
                f"Execution '{execution_id}' is already being resumed or is no longer failed"
            ),
        }

    # Reset the resume point and all subsequent steps to PENDING only after the claim.
    try:
        reset_count = execution_manager.reset_steps_from(execution_id, resume_step_id)
    except Exception:
        execution_manager.update_execution_status(execution_id, ExecutionStatus.FAILED)
        raise

    task = asyncio.create_task(
        _execute_pipeline_background(
            executor,
            pipeline,
            inputs,
            project_id,
            execution_id,
            execution.pipeline_name,
            session_id=session_id or execution.session_id,
        ),
        name=f"pipeline-resume-{execution.pipeline_name}-{execution_id[:8]}",
    )
    _register_background_task(execution_id, task)

    return {
        "success": True,
        "status": "resuming",
        "execution_id": execution_id,
        "reset_from_step": resume_step_id,
        "steps_reset": reset_count,
        "message": (
            f"Pipeline '{execution.pipeline_name}' resuming from step '{resume_step_id}' "
            f"({reset_count} step(s) reset). You will be notified when it completes."
        ),
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
    *,
    run_db: RunDb | None = None,
) -> list[str]:
    """Resume pipelines that were running when the daemon last stopped.

    Finds RUNNING executions whose pipeline definition has resume_on_restart=True,
    re-queues them as background tasks using the existing resume path (execution_id),
    and returns the list of resumed execution IDs. Non-resumable executions are left
    RUNNING so the caller can mark them via
    interrupt_stale_running_executions(exclude_ids=...).

    Args:
        loader: PipelineLoader for loading pipeline definitions.
        executor: PipelineExecutor instance.
        execution_manager: LocalPipelineExecutionManager instance.
        project_id: Current project ID (unused; each execution resumes under
            its own stored project_id).

    Returns:
        List of execution IDs that were successfully re-queued.
    """
    from gobby.workflows.pipeline_state import ExecutionStatus

    running_executions: list[Any] = []
    offset = 0
    page_size = 100
    while True:
        running = await _run_sync_db(
            run_db,
            execution_manager.list_executions,
            status=ExecutionStatus.RUNNING,
            limit=page_size,
            offset=offset,
        )
        running_executions.extend(running)
        offset += len(running)
        if len(running) < page_size:
            break

    resumed: list[str] = []
    for execution in running_executions:
        try:
            pipeline = await loader.load_pipeline(
                execution.pipeline_name,
                project_path=execution.project_id,
            )
        except Exception as e:
            logger.warning(
                "Cannot load pipeline '%s' for execution %s — will be interrupted: %s",
                execution.pipeline_name,
                execution.id,
                e,
            )
            continue

        if not pipeline:
            continue

        if not pipeline.enabled:
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

        # Re-queue as background task with existing execution_id (resume path).
        # Use the execution's own project_id (NOT NULL uuid) rather than the
        # daemon's current project — never bind "" against uuid columns.
        task = asyncio.create_task(
            _execute_pipeline_background(
                executor,
                pipeline,
                inputs,
                execution.project_id,
                execution.id,
                execution.pipeline_name,
                session_id=execution.session_id,
            ),
            name=f"pipeline-resume-{execution.pipeline_name}-{execution.id[:8]}",
        )
        _register_background_task(execution.id, task)
        resumed.append(execution.id)
        logger.info("Resumed pipeline '%s' execution %s", execution.pipeline_name, execution.id)

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

        # Parse review if available
        review = None
        if execution.review_json:
            try:
                review = json.loads(execution.review_json)
            except json.JSONDecodeError:
                review = execution.review_json

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
            "session_id": execution.session_id,
            "review": review,
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
