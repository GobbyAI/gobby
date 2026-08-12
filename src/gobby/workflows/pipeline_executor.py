"""Pipeline executor for running typed pipeline workflows."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast

from opentelemetry.trace import Status, StatusCode

from gobby.config.pipelines import PipelineConfig
from gobby.telemetry.tracing import create_span
from gobby.utils.json_helpers import json_dumps
from gobby.workflows.pipeline.gatekeeper import ApprovalManager
from gobby.workflows.pipeline.handlers import (
    execute_exec_step,
    execute_mcp_step,
    execute_prompt_step,
)
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_executor_events import PipelineExecutorEventsMixin
from gobby.workflows.pipeline_executor_outputs import (
    PipelineExecutorOutputMixin,
    _coerce_rendered_value,
)
from gobby.workflows.pipeline_executor_steps import PipelineExecutorStepMixin
from gobby.workflows.pipeline_state import (
    ApprovalRequired,
    ExecutionStatus,
    PipelineExecution,
    StepExecution,
    StepStatus,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.workflows.definitions import PipelineDefinition
    from gobby.workflows.templates import TemplateEngine

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")


# Type alias for event callback
PipelineEventCallback = Callable[..., Awaitable[None]]

__all__ = [
    "PipelineEventCallback",
    "PipelineExecutor",
    "_coerce_rendered_value",
    "execute_exec_step",
    "execute_mcp_step",
    "execute_prompt_step",
]


def _best_effort_child_session_setup(action: Callable[[], Any], warning: str) -> None:
    try:
        action()
    except Exception:
        logger.warning(warning, exc_info=True)


class PipelineExecutor(
    PipelineExecutorEventsMixin,
    PipelineExecutorStepMixin,
    PipelineExecutorOutputMixin,
):
    """Executor for pipeline workflows with typed data flow between steps.

    Handles:
    - Creating and tracking execution records
    - Iterating through steps in order
    - Building context with inputs and step outputs
    - Executing exec commands, prompts, and nested pipelines
    - Webhook notifications
    - WebSocket event broadcasting for real-time updates
    """

    def __init__(
        self,
        db: HubDatabase,
        execution_manager: LocalPipelineExecutionManager,
        llm_service: Any,
        template_engine: TemplateEngine | None = None,
        webhook_notifier: Any | None = None,
        loader: Any | None = None,
        event_callback: PipelineEventCallback | None = None,
        tool_proxy_getter: Any | None = None,
        session_manager: Any | None = None,
        completion_registry: Any | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
        pipeline_config: PipelineConfig | None = None,
        pipeline_config_resolver: Callable[[], PipelineConfig | None] | None = None,
        llm_service_resolver: Callable[[], Any] | None = None,
    ) -> None:
        """Initialize the pipeline executor.

        Args:
            db: Database connection for transactions
            execution_manager: Manager for pipeline execution records
            llm_service: LLM service for prompt steps
            template_engine: Optional template engine for variable substitution
            webhook_notifier: Optional notifier for webhook callbacks
            loader: Optional workflow loader for nested pipelines
            event_callback: Optional async callback for broadcasting events.
                           Signature: async def callback(event: str, execution_id: str, **kwargs)
            tool_proxy_getter: Optional callable returning ToolProxyService for MCP steps
            session_manager: Optional SessionManager for session creation
            completion_registry: Optional CompletionEventRegistry for wait steps
            run_db: Optional bounded executor bridge for hub database work
            pipeline_config: Optional pipeline configuration for step defaults
            pipeline_config_resolver: Resolves current pipeline configuration
            llm_service_resolver: Resolves the current LLM service for prompt steps
        """
        self.db = db
        self.execution_manager = execution_manager
        self.llm_service = llm_service
        self._llm_service_resolver = llm_service_resolver or (lambda: self.llm_service)
        self.webhook_notifier = webhook_notifier
        self.loader = loader
        self.event_callback = event_callback
        self.tool_proxy_getter = tool_proxy_getter
        self.session_manager = session_manager
        self.completion_registry = completion_registry
        self.run_db = run_db
        self._pipeline_config = pipeline_config or PipelineConfig()
        self._pipeline_config_resolver = pipeline_config_resolver or (lambda: self._pipeline_config)
        self._pipeline_config_context: ContextVar[PipelineConfig | None] = ContextVar(
            "gobby_pipeline_config", default=None
        )

        self.renderer = StepRenderer(template_engine)
        self.approval_manager = ApprovalManager(
            execution_manager=execution_manager,
            webhook_notifier=webhook_notifier,
            event_callback=event_callback,
            run_db=run_db,
        )

        # Detached runs (start_detached): tasks are retained so they are not
        # garbage-collected mid-run, and their execution IDs let the startup
        # sweep tell live runs apart from restart orphans.
        self._detached_tasks: set[asyncio.Task[Any]] = set()
        self._detached_execution_ids: set[str] = set()

    @property
    def pipeline_config(self) -> PipelineConfig:
        return (
            self._pipeline_config_context.get()
            or self._pipeline_config_resolver()
            or self._pipeline_config
        )

    async def _run_db(self, func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
        """Run synchronous storage work through the configured database executor."""
        if self.run_db:
            return cast(_T, await self.run_db(func, *args, **kwargs))
        return await asyncio.to_thread(func, *args, **kwargs)

    def _create_execution_record(
        self,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        session_id: str | None,
        project_id: str,
    ) -> PipelineExecution:
        """Create an execution record with a snapshot of the definition."""
        try:
            definition_snapshot = pipeline.model_dump_json()
        except Exception:
            definition_snapshot = json_dumps(
                {"name": pipeline.name, "error": "serialization failed"}
            )
        execution: PipelineExecution = self.execution_manager.create_execution(
            pipeline_name=pipeline.name,
            inputs_json=json_dumps(inputs),
            session_id=session_id,
            definition_json=definition_snapshot,
            project_id=project_id,
        )
        return execution

    async def start_detached(
        self,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        project_id: str,
        session_id: str | None = None,
    ) -> PipelineExecution:
        """Start a pipeline execution without awaiting its completion.

        Creates the execution record, marks it RUNNING so callers observe a
        live execution immediately (no scheduling race), and hands the actual
        run to a retained background task. Progress is observable through the
        usual pipeline_event broadcasts and the executions API.

        Returns:
            The RUNNING PipelineExecution record.
        """
        if not pipeline.enabled:
            raise ValueError(f"Pipeline '{pipeline.name}' is disabled")

        execution = await self._run_db(
            self._create_execution_record, pipeline, inputs, session_id, project_id
        )
        updated = await self._run_db(
            self.execution_manager.update_execution_status,
            execution_id=execution.id,
            status=ExecutionStatus.RUNNING,
        )
        if updated:
            execution = updated

        task = asyncio.create_task(
            self.execute(
                pipeline=pipeline,
                inputs=inputs,
                project_id=project_id,
                execution_id=execution.id,
                session_id=session_id,
            ),
            name=f"pipeline-detached-{execution.id}",
        )
        self._track_detached_task(task, execution.id)
        return execution

    def _track_detached_task(self, task: asyncio.Task[Any], execution_id: str) -> None:
        self._detached_tasks.add(task)
        self._detached_execution_ids.add(execution_id)

        def _on_done(done: asyncio.Task[Any]) -> None:
            self._detached_tasks.discard(done)
            self._detached_execution_ids.discard(execution_id)
            if done.cancelled():
                return
            exc = done.exception()
            if isinstance(exc, ApprovalRequired):
                # A detached run reaching an approval gate is parked, not
                # broken; approval resumes it through the normal flow.
                logger.info(
                    "Detached pipeline run %s is waiting for approval (step %s)",
                    execution_id,
                    exc.step_id,
                )
            elif exc:
                logger.error("Detached pipeline run %s failed: %s", execution_id, exc)

        task.add_done_callback(_on_done)

    def startup_sweep(self) -> int:
        """Mark restart-orphaned RUNNING executions FAILED.

        A freshly created executor owns no background tasks, so any RUNNING
        execution in its manager's scope was orphaned by a daemon restart —
        detached runs and approval-resumed runs alike. Without this, clients
        polling the executions API would watch a phantom RUNNING execution
        forever. Live detached runs are excluded, so the sweep is safe to run
        at any point in the executor's lifetime.

        Returns:
            Number of executions marked as failed.
        """
        count: int = self.execution_manager.fail_stale_running_executions(
            exclude_ids=set(self._detached_execution_ids)
        )
        if count > 0:
            logger.info("Startup sweep marked %s orphaned pipeline execution(s) failed", count)
        return count

    def _get_cancelled_execution(self, execution_id: str) -> PipelineExecution | None:
        """Return the latest execution record when cancellation was persisted."""
        execution: PipelineExecution | None = self.execution_manager.get_execution(execution_id)
        if execution and execution.status == ExecutionStatus.CANCELLED:
            return execution
        return None

    async def execute(
        self,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        project_id: str,
        execution_id: str | None = None,
        session_id: str | None = None,
        _depth: int = 0,
        _pipeline_stack: frozenset[str] | None = None,
        _parent_session_id: str | None = None,
    ) -> PipelineExecution:
        """Execute one pipeline against a single runtime configuration epoch."""
        token = self._pipeline_config_context.set(
            self._pipeline_config_context.get()
            or self._pipeline_config_resolver()
            or self._pipeline_config
        )
        try:
            return await self._execute(
                pipeline,
                inputs,
                project_id,
                execution_id,
                session_id,
                _depth,
                _pipeline_stack,
                _parent_session_id,
            )
        finally:
            self._pipeline_config_context.reset(token)

    async def _execute(
        self,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        project_id: str,
        execution_id: str | None = None,
        session_id: str | None = None,
        _depth: int = 0,
        _pipeline_stack: frozenset[str] | None = None,
        _parent_session_id: str | None = None,
    ) -> PipelineExecution:
        """Execute a pipeline workflow.

        Args:
            pipeline: The pipeline definition to execute
            inputs: Input values for the pipeline
            project_id: Project context for the execution
            execution_id: Optional existing execution ID (for resuming)
            session_id: Optional session that triggered the execution
            _parent_session_id: Original caller's session ID (for nested pipelines)

        Returns:
            The completed PipelineExecution record

        Raises:
            RuntimeError: If nesting depth limit exceeded or cycle detected
        """
        if not pipeline.enabled:
            raise ValueError(f"Pipeline '{pipeline.name}' is disabled")

        span_attrs = {
            "pipeline_name": pipeline.name,
            "project_id": project_id,
        }
        if execution_id:
            span_attrs["execution_id"] = execution_id

        # Track current step for error handling
        current_step_execution: StepExecution | None = None
        execution: PipelineExecution | None = None
        caller_session_id: str | None = session_id
        pipeline_session_id: str | None = session_id

        with create_span("pipeline.execute", attributes=span_attrs) as span:
            try:
                # 0. Enforce nesting depth limit and cycle detection
                depth_limit = self.pipeline_config.nesting_depth_limit

                if _depth > depth_limit:
                    raise RuntimeError(
                        f"Pipeline nesting depth limit exceeded ({_depth} > {depth_limit}). "
                        f"Pipeline '{pipeline.name}' would exceed maximum recursion depth."
                    )

                if _pipeline_stack is None:
                    _pipeline_stack = frozenset()

                # Block cross-pipeline cycles (A→B→A) but allow self-recursion (A→A).
                # Self-recursion is bounded by the depth limit above.
                if pipeline.name in _pipeline_stack and _pipeline_stack != frozenset(
                    {pipeline.name}
                ):
                    raise RuntimeError(
                        f"Pipeline cycle detected: '{pipeline.name}' is already in the "
                        f"call stack {sorted(_pipeline_stack)}."
                    )

                _pipeline_stack = _pipeline_stack | {pipeline.name}

                # 1. Create or load execution record
                _terminal_statuses = {ExecutionStatus.CANCELLED, ExecutionStatus.COMPLETED}
                prior_status: ExecutionStatus | None = None
                if execution_id:
                    execution = await self._run_db(
                        self.execution_manager.get_execution, execution_id
                    )
                    if not execution:
                        raise ValueError(f"Execution {execution_id} not found")
                    prior_status = execution.status
                    if prior_status in _terminal_statuses:
                        raise ValueError(
                            f"Cannot resume execution {execution_id}: "
                            f"status is {prior_status.value} (terminal). "
                            f"Start a new execution instead."
                        )
                else:
                    execution = await self._run_db(
                        self._create_execution_record,
                        pipeline,
                        inputs,
                        session_id,
                        project_id,
                    )
                    if span.is_recording():
                        span.set_attribute("execution_id", str(execution.id))
                    execution_id = execution.id

                # 2. Update status to RUNNING. Failed resumes use an atomic claim
                # so concurrent callers cannot both reset and execute the same rows.
                if prior_status == ExecutionStatus.FAILED:
                    updated = await self._run_db(
                        self.execution_manager.claim_failed_execution_for_resume, execution.id
                    )
                    if updated is None:
                        execution = None
                        raise ValueError(
                            f"Cannot resume execution {execution_id}: it is already being resumed"
                        )
                else:
                    updated = await self._run_db(
                        self.execution_manager.update_execution_status,
                        execution_id=execution.id,
                        status=ExecutionStatus.RUNNING,
                    )
                if updated:
                    execution = updated

                # Emit pipeline_started event
                await self._emit_event(
                    "pipeline_started",
                    execution.id,
                    pipeline_name=pipeline.name,
                    inputs=inputs,
                    step_count=len(pipeline.steps),
                )

                # 2b. Create child session for top-level pipelines
                from gobby.storage.sessions import system_session_id

                caller_session_id = session_id or system_session_id()
                pipeline_session_id = caller_session_id
                parent_session_id = _parent_session_id

                if _depth == 0 and self.session_manager:
                    try:
                        child_session = await self._run_db(
                            self.session_manager.register,
                            external_id=f"pipeline-{execution.id}",
                            machine_id=None,
                            source="pipeline",
                            project_id=project_id,
                            parent_session_id=caller_session_id,
                            agent_depth=0,
                        )
                        pipeline_session_id = child_session.id
                        parent_session_id = caller_session_id
                        # Mark the session as a deterministic executor so
                        # LLM-behavior rules (skill gates, audience filters)
                        # can tell it apart from agent sessions.
                        from gobby.workflows.state_manager import SessionVariableManager

                        await self._run_db(
                            _best_effort_child_session_setup,
                            lambda: SessionVariableManager(self.db).set_variable(
                                child_session.id, "_agent_type", "pipeline"
                            ),
                            "Failed to mark pipeline child session agent type",
                        )
                        # The heartbeat resolves spawned agents through
                        # execution.session_id (list_by_parent); without the
                        # child session persisted, long wait steps get marked
                        # FAILED as "stalled, no agents" while the agent runs.
                        await self._run_db(
                            _best_effort_child_session_setup,
                            lambda: self.execution_manager.update_execution_session(
                                execution_id, child_session.id
                            ),
                            "Failed to persist pipeline child session on execution",
                        )
                        logger.info(
                            "Created child session %s for pipeline %s (parent=%s)",
                            child_session.id,
                            pipeline.name,
                            caller_session_id,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to create child session for pipeline, using caller session_id",
                            exc_info=True,
                        )

                # 3. Build execution context (resolve defaults from pipeline input definitions)
                resolved_defaults: dict[str, Any] = {}
                for key, spec in pipeline.inputs.items():
                    if isinstance(spec, dict):
                        resolved_defaults[key] = spec.get("default")
                    else:
                        # Bare value (not a definition dict) — use as-is
                        resolved_defaults[key] = spec
                merged_inputs = {**resolved_defaults, **inputs}
                # Inject parent_session_id into inputs so ${{ inputs.parent_session_id }} resolves
                if parent_session_id and not inputs.get("parent_session_id"):
                    merged_inputs["parent_session_id"] = parent_session_id
                # Inject session_id into inputs so ${{ inputs.session_id }} resolves
                if pipeline_session_id and "session_id" not in merged_inputs:
                    merged_inputs["session_id"] = pipeline_session_id
                # Resolve project context for template expressions
                project_path: str | None = None
                current_branch: str | None = None
                try:
                    from gobby.utils.project_context import get_project_context

                    pctx = get_project_context()
                    if pctx:
                        project_path = pctx.get("project_path")
                except (ImportError, OSError):
                    pass

                if project_path:
                    try:
                        from gobby.worktrees.git import WorktreeGitManager

                        current_branch = WorktreeGitManager(project_path).get_current_branch()
                    except (ImportError, ValueError, OSError):
                        pass

                context: dict[str, Any] = {
                    "inputs": merged_inputs,
                    "steps": {},  # Will hold step outputs as they complete
                    "session_id": pipeline_session_id,
                    "parent_session_id": parent_session_id,
                    "project_id": project_id,
                    "project_path": project_path,
                    "current_branch": current_branch or "main",
                    "_depth": _depth,
                    "_pipeline_stack": _pipeline_stack,
                }

                # Fetch existing steps when resuming. Failed executions reset all
                # persisted steps so they re-execute without creating duplicate rows.
                existing_steps: dict[str, StepExecution] = {}
                if execution_id:
                    steps = await self._run_db(
                        self.execution_manager.get_steps_for_execution, execution_id
                    )
                    if prior_status == ExecutionStatus.FAILED and steps:
                        await self._run_db(
                            self.execution_manager.reset_steps_from,
                            execution_id,
                            steps[0].step_id,
                        )
                        steps = await self._run_db(
                            self.execution_manager.get_steps_for_execution, execution_id
                        )
                    existing_steps = {s.step_id: s for s in steps}

                # 4. Iterate through steps in order
                for step in pipeline.steps:
                    cancelled = await self._run_db(self._get_cancelled_execution, execution.id)
                    if cancelled:
                        self._close_pipeline_session(pipeline_session_id, caller_session_id)
                        return cancelled

                    # Check for existing execution
                    step_execution = existing_steps.get(step.id)

                    if step_execution:
                        # If completed, load output into context and skip
                        if step_execution.status == StepStatus.COMPLETED:
                            logger.info("Skipping completed step %s", step.id)
                            output = None
                            if step_execution.output_json:
                                try:
                                    output = json.loads(step_execution.output_json)
                                except json.JSONDecodeError:
                                    output = step_execution.output_json
                            context["steps"][step.id] = {"output": output}
                            continue

                        # If skipped, just skip (but register in context so downstream
                        # conditions like ``steps.X.output`` resolve to None instead
                        # of raising a KeyError / attribute error).
                        if step_execution.status == StepStatus.SKIPPED:
                            logger.info("Skipping previously skipped step %s", step.id)
                            context["steps"][step.id] = {"output": None}
                            continue

                        # A still-waiting step will raise ApprovalRequired again.
                        # Approved steps are reset to PENDING and execute below.
                        if step_execution.status == StepStatus.WAITING_APPROVAL:
                            pass

                    # Create new step execution if not exists
                    if not step_execution:
                        step_execution = await self._run_db(
                            self.execution_manager.create_step_execution,
                            execution_id=execution.id,
                            step_id=step.id,
                            input_json=json_dumps(
                                {k: v for k, v in context.items() if not k.startswith("_")}
                            )
                            if context
                            else None,
                        )
                    assert step_execution is not None

                    # Check if step should run based on condition
                    if not self.renderer.should_run_step(step, context):
                        # Skip this step
                        await self._run_db(
                            self.execution_manager.update_step_execution,
                            step_execution_id=step_execution.id,
                            status=StepStatus.SKIPPED,
                        )
                        logger.info("Skipping step %s: condition not met", step.id)

                        # Emit step_skipped event
                        await self._emit_event(
                            "step_skipped",
                            execution.id,
                            step_id=step.id,
                            step_name=getattr(step, "name", step.id),
                            reason="condition not met",
                        )
                        # Register skipped step in context so downstream conditions
                        # like ``steps.X.output`` resolve to None instead of erroring.
                        context["steps"][step.id] = {"output": None}
                        current_step_execution = None
                        continue

                    # Update step status to RUNNING
                    await self._run_db(
                        self.execution_manager.update_step_execution,
                        step_execution_id=step_execution.id,
                        status=StepStatus.RUNNING,
                    )
                    step_execution.status = StepStatus.RUNNING
                    current_step_execution = step_execution

                    # Emit step_started event
                    await self._emit_event(
                        "step_started",
                        execution.id,
                        step_id=step.id,
                        step_name=getattr(step, "name", step.id),
                    )

                    # Check for approval gate
                    await self.approval_manager.check_approval_gate(
                        step, execution, step_execution, pipeline
                    )

                    # Execute the step
                    step_output = await self._execute_step(step, context, project_id)

                    cancelled = await self._run_db(self._get_cancelled_execution, execution.id)
                    if cancelled:
                        current_step_execution = None
                        self._close_pipeline_session(pipeline_session_id, caller_session_id)
                        return cancelled

                    # Detect exec step failures from non-zero exit codes
                    if isinstance(step_output, dict) and step_output.get("exit_code", 0) != 0:
                        error_msg = (
                            step_output.get("stderr")
                            or step_output.get("stdout")
                            or "Unknown error"
                        )
                        context["steps"][step.id] = {"output": step_output}
                        await self._run_db(
                            self.execution_manager.update_step_execution,
                            step_execution_id=step_execution.id,
                            status=StepStatus.FAILED,
                            output_json=json_dumps(step_output),
                            error=f"Exit code {step_output['exit_code']}: {error_msg}",
                        )
                        raise RuntimeError(
                            f"Step '{step.id}' failed with exit code {step_output['exit_code']}"
                        )

                    if isinstance(step_output, dict) and "error" in step_output:
                        error_msg = str(step_output["error"])
                        context["steps"][step.id] = {"output": step_output}
                        await self._run_db(
                            self.execution_manager.update_step_execution,
                            step_execution_id=step_execution.id,
                            status=StepStatus.FAILED,
                            output_json=json_dumps(step_output),
                            error=error_msg,
                        )
                        raise RuntimeError(f"Step '{step.id}' failed: {error_msg}")

                    # For exec steps with JSON stdout, merge parsed data into output
                    if isinstance(step_output, dict) and "stdout" in step_output:
                        try:
                            parsed = json.loads(step_output["stdout"].strip())
                            if isinstance(parsed, dict):
                                step_output.update(parsed)
                        except (json.JSONDecodeError, ValueError):
                            pass

                    # Store step output in context for subsequent steps
                    context["steps"][step.id] = {"output": step_output}

                    # Update step with output and mark completed
                    await self._run_db(
                        self.execution_manager.update_step_execution,
                        step_execution_id=step_execution.id,
                        status=StepStatus.COMPLETED,
                        output_json=json_dumps(step_output) if step_output is not None else None,
                    )
                    current_step_execution = None

                    # Emit step_completed event
                    await self._emit_event(
                        "step_completed",
                        execution.id,
                        step_id=step.id,
                        step_name=getattr(step, "name", step.id),
                        output=step_output,
                    )

                cancelled = await self._run_db(self._get_cancelled_execution, execution.id)
                if cancelled:
                    self._close_pipeline_session(pipeline_session_id, caller_session_id)
                    return cancelled

                # 5. Safety net — verify no steps failed before marking completed
                failed_steps = await self._run_db(
                    self.execution_manager.get_failed_steps, execution.id
                )
                if failed_steps:
                    failed_ids = [s.step_id for s in failed_steps]
                    outputs = self._build_outputs(pipeline, context)
                    await self._run_db(
                        self.execution_manager.update_execution_status,
                        execution_id=execution.id,
                        status=ExecutionStatus.FAILED,
                        outputs_json=json_dumps(outputs),
                    )
                    raise RuntimeError(f"Pipeline has failed steps: {', '.join(failed_ids)}")

                # Mark execution as completed
                outputs = self._build_outputs(pipeline, context)
                completed = await self._run_db(
                    self.execution_manager.update_execution_status,
                    execution_id=execution.id,
                    status=ExecutionStatus.COMPLETED,
                    outputs_json=json_dumps(outputs),
                )
                if completed:
                    execution = completed

                if self.webhook_notifier:
                    await self.webhook_notifier.notify_complete(
                        execution=execution,
                        pipeline=pipeline,
                    )

                # Emit pipeline_completed event
                await self._emit_event(
                    "pipeline_completed",
                    execution.id,
                    pipeline_name=pipeline.name,
                    outputs=outputs,
                )

                # Notify completion registry
                await self._notify_completion(
                    execution.id, "completed", pipeline.name, outputs=outputs
                )

                if span.is_recording():
                    span.set_attribute("status", "completed")
                    span.set_attribute("step_count", len(pipeline.steps))

                # Close pipeline session (implementation detail, not user-facing)
                self._close_pipeline_session(pipeline_session_id, caller_session_id)

                return execution

            except asyncio.CancelledError:
                self._close_pipeline_session(pipeline_session_id, caller_session_id)
                raise

            except ApprovalRequired:
                # Don't treat approval as an error - just re-raise
                raise

            except Exception as e:
                if span.is_recording():
                    span.set_attribute("status", "failed")
                    span.set_attribute("step_count", len(pipeline.steps))
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))

                if execution:
                    logger.exception("Pipeline execution failed: %s", e)

                    # Mark the currently-running step as FAILED
                    if (
                        current_step_execution
                        and current_step_execution.status == StepStatus.RUNNING
                    ):
                        try:
                            await self._run_db(
                                self.execution_manager.update_step_execution,
                                step_execution_id=current_step_execution.id,
                                status=StepStatus.FAILED,
                                error=str(e),
                            )
                        except Exception:
                            logger.exception(
                                "Failed to mark step %s as failed",
                                current_step_execution.id,
                            )

                    try:
                        failed = await self._run_db(
                            self.execution_manager.update_execution_status,
                            execution_id=execution.id,
                            status=ExecutionStatus.FAILED,
                            outputs_json=json_dumps({"error": str(e)}),
                        )
                        if failed:
                            execution = failed
                    except Exception:
                        logger.exception(
                            "Failed to mark execution %s as failed",
                            execution.id,
                        )

                    if self.webhook_notifier:
                        await self.webhook_notifier.notify_failure(
                            execution=execution,
                            pipeline=pipeline,
                            error=str(e),
                        )

                    # Emit pipeline_failed event
                    await self._emit_event(
                        "pipeline_failed",
                        execution.id,
                        pipeline_name=pipeline.name,
                        error=str(e),
                    )

                    # Notify completion registry
                    await self._notify_completion(
                        execution.id, "failed", pipeline.name, error=str(e)
                    )

                    # Close pipeline session on failure too
                    self._close_pipeline_session(pipeline_session_id, caller_session_id)
                raise
