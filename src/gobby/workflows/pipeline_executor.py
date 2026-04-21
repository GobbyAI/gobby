"""Pipeline executor for running typed pipeline workflows."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, cast

from opentelemetry.trace import Status, StatusCode

from gobby.telemetry.tracing import create_span
from gobby.workflows.pipeline.gatekeeper import ApprovalManager
from gobby.workflows.pipeline.handlers import (
    execute_exec_step,
    execute_mcp_step,
    execute_prompt_step,
)
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_state import (
    ApprovalRequired,
    ExecutionStatus,
    PipelineExecution,
    StepExecution,
    StepStatus,
)

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.workflows.definitions import PipelineDefinition
    from gobby.workflows.templates import TemplateEngine

logger = logging.getLogger(__name__)


# Type alias for event callback
PipelineEventCallback = Any  # Callable[[str, str, dict], Awaitable[None]]


def _coerce_rendered_value(value: Any) -> Any:
    """Coerce Jinja2-rendered string values back to native Python types.

    Jinja2 renders everything as strings, so boolean False becomes "False"
    which is truthy. This coerces known literals back to their native types.
    """
    if not isinstance(value, str):
        return value
    lower = value.strip().lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower == "none":
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


class PipelineExecutor:
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
        db: DatabaseProtocol,
        execution_manager: LocalPipelineExecutionManager,
        llm_service: Any,
        template_engine: TemplateEngine | None = None,
        webhook_notifier: Any | None = None,
        loader: Any | None = None,
        event_callback: PipelineEventCallback | None = None,
        tool_proxy_getter: Any | None = None,
        session_manager: Any | None = None,
        completion_registry: Any | None = None,
    ):
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
            session_manager: Optional LocalSessionManager for session creation
            completion_registry: Optional CompletionEventRegistry for wait steps
        """
        self.db = db
        self.execution_manager = execution_manager
        self.llm_service = llm_service
        self.webhook_notifier = webhook_notifier
        self.loader = loader
        self.event_callback = event_callback
        self.tool_proxy_getter = tool_proxy_getter
        self.session_manager = session_manager
        self.completion_registry = completion_registry

        self.renderer = StepRenderer(template_engine)
        self.approval_manager = ApprovalManager(
            execution_manager=execution_manager,
            webhook_notifier=webhook_notifier,
            event_callback=event_callback,
        )

    async def _emit_event(self, event: str, execution_id: str, **kwargs: Any) -> None:
        """Emit a pipeline event via the callback if configured.

        Args:
            event: Event type (pipeline_started, step_completed, etc.)
            execution_id: Pipeline execution ID
            **kwargs: Additional event data
        """
        if self.event_callback:
            try:
                await self.event_callback(event, execution_id, **kwargs)
            except (ValueError, RuntimeError, OSError):
                logger.warning(
                    "Failed to emit pipeline event",
                    extra={"event": event, "execution_id": execution_id},
                    exc_info=True,
                )

    async def _notify_completion(
        self,
        execution_id: str,
        status: str,
        pipeline_name: str,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Notify the completion registry that a pipeline finished.

        Fail-open: errors are logged but never propagate to the caller.
        """
        if not self.completion_registry:
            return
        try:
            result: dict[str, Any] = {
                "status": status,
                "pipeline_name": pipeline_name,
            }
            if outputs is not None:
                result["outputs"] = outputs
            if error is not None:
                result["error"] = error

            # Build targeted message for orchestration completion
            message = ""
            if outputs and str(outputs.get("orchestration_complete", "")).lower() in (
                "true",
                "1",
            ):
                session_task = outputs.get("session_task", "unknown")
                iteration = outputs.get("iteration", "?")
                message = (
                    f"Orchestration complete for task {session_task}. "
                    f"All tasks finished after {iteration} iterations."
                )

            await self.completion_registry.notify(execution_id, result, message=message)
        except Exception:
            logger.warning(
                f"Failed to notify completion registry for {execution_id}",
                exc_info=True,
            )

    def _close_pipeline_session(
        self,
        pipeline_session_id: str | None,
        caller_session_id: str | None,
    ) -> None:
        """Close the pipeline's child session after execution finishes.

        Pipeline sessions are implementation details — they should not
        linger in the user's session list.  Fail-open: errors are logged
        but never propagate.
        """
        if (
            not pipeline_session_id
            or not self.session_manager
            or pipeline_session_id == caller_session_id
        ):
            return
        try:
            self.session_manager.update_status(pipeline_session_id, "deleted")
        except Exception:
            logger.warning(
                f"Failed to close pipeline session {pipeline_session_id}",
                exc_info=True,
            )

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
                depth_limit = 10
                try:
                    from gobby.config.pipelines import PipelineConfig

                    depth_limit = PipelineConfig().nesting_depth_limit
                except (ImportError, ValueError):
                    pass

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
                    execution = self.execution_manager.get_execution(execution_id)
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
                    # Snapshot the pipeline definition for later inspection
                    try:
                        definition_snapshot = pipeline.model_dump_json()
                    except Exception:
                        definition_snapshot = json.dumps(
                            {"name": pipeline.name, "error": "serialization failed"}
                        )
                    execution = self.execution_manager.create_execution(
                        pipeline_name=pipeline.name,
                        inputs_json=json.dumps(inputs),
                        session_id=session_id,
                        definition_json=definition_snapshot,
                    )
                    if span.is_recording():
                        span.set_attribute("execution_id", str(execution.id))

                # 2. Update status to RUNNING
                updated = self.execution_manager.update_execution_status(
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
                from gobby.storage.sessions import SYSTEM_SESSION_ID

                caller_session_id = session_id or SYSTEM_SESSION_ID
                pipeline_session_id = caller_session_id
                parent_session_id = _parent_session_id

                if _depth == 0 and self.session_manager:
                    try:
                        child_session = self.session_manager.register(
                            external_id=f"pipeline-{execution.id}",
                            machine_id="pipeline",
                            source="pipeline",
                            project_id=project_id,
                            title=f"pipeline:{pipeline.name}",
                            parent_session_id=caller_session_id,
                            agent_depth=0,
                        )
                        pipeline_session_id = child_session.id
                        parent_session_id = caller_session_id
                        logger.info(
                            f"Created child session {child_session.id} for pipeline "
                            f"{pipeline.name} (parent={caller_session_id})"
                        )
                    except Exception:
                        logger.warning(
                            "Failed to create child session for pipeline, using caller session_id",
                            exc_info=True,
                        )
                    else:
                        # Exempt pipeline steps from progressive-discovery rules
                        # without depending on event-time source resolution, which
                        # races against the just-written session row (see #12135).
                        try:
                            from gobby.workflows.state_manager import (
                                SessionVariableManager,
                            )

                            SessionVariableManager(self.session_manager.db).merge_variables(
                                child_session.id,
                                {"enforce_tool_schema_check": False},
                            )
                        except Exception:
                            logger.warning(
                                "Failed to seed enforce_tool_schema_check=False "
                                "on pipeline session %s; progressive-discovery "
                                "rules may block pipeline steps",
                                child_session.id,
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

                # Fetch existing steps if resuming a non-failed execution.
                # Failed executions re-execute all steps since completed steps
                # may reference stale agent sessions or other invalidated state.
                existing_steps: dict[str, StepExecution] = {}
                if execution_id and prior_status != ExecutionStatus.FAILED:
                    steps = self.execution_manager.get_steps_for_execution(execution_id)
                    existing_steps = {s.step_id: s for s in steps}

                # 4. Iterate through steps in order
                for step in pipeline.steps:
                    # Check for existing execution
                    step_execution = existing_steps.get(step.id)

                    if step_execution:
                        # If completed, load output into context and skip
                        if step_execution.status == StepStatus.COMPLETED:
                            logger.info(f"Skipping completed step {step.id}")
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
                            logger.info(f"Skipping previously skipped step {step.id}")
                            context["steps"][step.id] = {"output": None}
                            continue

                        # If waiting approval, check if we should check gate again
                        # If we are resuming, it might have been approved
                        if step_execution.status == StepStatus.WAITING_APPROVAL:
                            # If the step is still marked as waiting approval in DB,
                            # checking the gate will just re-raise ApprovalRequired.
                            # If it was approved, status should be COMPLETED.
                            # So we can just proceed to check/execute.
                            pass

                    # Create new step execution if not exists
                    if not step_execution:
                        step_execution = self.execution_manager.create_step_execution(
                            execution_id=execution.id,
                            step_id=step.id,
                            input_json=json.dumps(
                                {k: v for k, v in context.items() if not k.startswith("_")}
                            )
                            if context
                            else None,
                        )

                    # Check if step should run based on condition
                    if not self.renderer.should_run_step(step, context):
                        # Skip this step
                        self.execution_manager.update_step_execution(
                            step_execution_id=step_execution.id,
                            status=StepStatus.SKIPPED,
                        )
                        logger.info(f"Skipping step {step.id}: condition not met")

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
                    self.execution_manager.update_step_execution(
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

                    # Detect exec step failures from non-zero exit codes
                    if isinstance(step_output, dict) and step_output.get("exit_code", 0) != 0:
                        error_msg = (
                            step_output.get("stderr")
                            or step_output.get("stdout")
                            or "Unknown error"
                        )
                        context["steps"][step.id] = {"output": step_output}
                        self.execution_manager.update_step_execution(
                            step_execution_id=step_execution.id,
                            status=StepStatus.FAILED,
                            output_json=json.dumps(step_output),
                            error=f"Exit code {step_output['exit_code']}: {error_msg}",
                        )
                        raise RuntimeError(
                            f"Step '{step.id}' failed with exit code {step_output['exit_code']}"
                        )

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
                    self.execution_manager.update_step_execution(
                        step_execution_id=step_execution.id,
                        status=StepStatus.COMPLETED,
                        output_json=json.dumps(step_output) if step_output is not None else None,
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

                # 5. Safety net — verify no steps failed before marking completed
                failed_steps = self.execution_manager.get_failed_steps(execution.id)
                if failed_steps:
                    failed_ids = [s.step_id for s in failed_steps]
                    outputs = self._build_outputs(pipeline, context)
                    self.execution_manager.update_execution_status(
                        execution_id=execution.id,
                        status=ExecutionStatus.FAILED,
                        outputs_json=json.dumps(outputs),
                    )
                    raise RuntimeError(f"Pipeline has failed steps: {', '.join(failed_ids)}")

                # Mark execution as completed
                outputs = self._build_outputs(pipeline, context)
                completed = self.execution_manager.update_execution_status(
                    execution_id=execution.id,
                    status=ExecutionStatus.COMPLETED,
                    outputs_json=json.dumps(outputs),
                )
                if completed:
                    execution = completed

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
                    logger.error(f"Pipeline execution failed: {e}", exc_info=True)

                    # Mark the currently-running step as FAILED
                    if (
                        current_step_execution
                        and current_step_execution.status == StepStatus.RUNNING
                    ):
                        try:
                            self.execution_manager.update_step_execution(
                                step_execution_id=current_step_execution.id,
                                status=StepStatus.FAILED,
                                error=str(e),
                            )
                        except Exception:
                            logger.error(
                                f"Failed to mark step {current_step_execution.id} as failed",
                                exc_info=True,
                            )

                    try:
                        failed = self.execution_manager.update_execution_status(
                            execution_id=execution.id,
                            status=ExecutionStatus.FAILED,
                            outputs_json=json.dumps({"error": str(e)}),
                        )
                        if failed:
                            execution = failed
                    except Exception:
                        logger.error(
                            f"Failed to mark execution {execution.id} as failed",
                            exc_info=True,
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

    async def _execute_step(
        self,
        step: Any,  # PipelineStep
        context: dict[str, Any],
        project_id: str,
    ) -> Any:
        """Execute a single pipeline step."""
        # Determine step type for instrumentation
        step_types = [
            f
            for f in (
                "wait",
                "exec",
                "prompt",
                "invoke_pipeline",
                "mcp",
                "activate_workflow",
            )
            if getattr(step, f, None)
        ]
        step_type = step_types[0] if step_types else "unknown"
        step_name = getattr(step, "name", step.id)

        with create_span(
            f"pipeline.step.{step.id}",
            attributes={"step_type": step_type, "step_name": step_name},
        ) as span:
            try:
                # Render any template variables in the step
                rendered_step = self.renderer.render_step(step, context)

                # Warn if multiple step types are set — only the first match executes
                if len(step_types) > 1:
                    logger.warning(
                        f"Step {step.id} has multiple types set: {step_types} - only '{step_types[0]}' will execute",
                    )

                if step.wait:
                    # Block until completion event fires
                    return await self._execute_wait_step(rendered_step, context)
                elif step.exec:
                    # Execute shell command
                    return await execute_exec_step(rendered_step.exec, context)
                elif step.prompt:
                    # Execute LLM prompt
                    return await execute_prompt_step(
                        rendered_step.prompt, context, self.llm_service
                    )
                elif step.invoke_pipeline:
                    # Execute nested pipeline
                    return await self._execute_nested_pipeline(
                        rendered_step.invoke_pipeline, context, project_id
                    )
                elif step.mcp:
                    # Execute MCP tool call
                    return await execute_mcp_step(
                        rendered_step,
                        context,
                        self.tool_proxy_getter,
                        self.session_manager,
                    )
                elif step.activate_workflow:
                    # activate_workflow steps are not supported in pipeline execution
                    return {
                        "error": "activate_workflow pipeline steps are not supported",
                        "step_id": step.id,
                    }
                else:
                    logger.warning(f"Step {step.id} has no action defined")
                    return None
            except Exception as e:
                if span.is_recording():
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                raise

    async def _execute_wait_step(
        self,
        rendered_step: Any,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a wait step by blocking on the completion registry.

        Args:
            rendered_step: Step with rendered template variables
            context: Execution context

        Returns:
            The completion result dict

        Raises:
            asyncio.TimeoutError: If timeout expires before completion
            RuntimeError: If no completion registry configured
        """
        wait_config = rendered_step.wait
        completion_id = wait_config.get("completion_id")
        timeout = wait_config.get("timeout", 600)

        if not completion_id:
            raise ValueError(f"wait step requires completion_id, got: {wait_config}")

        if not self.completion_registry:
            raise RuntimeError(
                f"wait step '{rendered_step.id}' requires a completion_registry "
                "but none is configured on the PipelineExecutor"
            )

        # Convert timeout to float
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            logger.warning(
                f"Invalid timeout value {timeout!r} for wait step '{rendered_step.id}', defaulting to 600s",
            )
            timeout = 600.0

        logger.info(
            f"Wait step '{rendered_step.id}' blocking on completion_id={completion_id} "
            f"(timeout={timeout}s)"
        )

        result = await self.completion_registry.wait(completion_id, timeout=timeout)
        return cast(dict[str, Any], result)

    async def approve(
        self,
        token: str,
        approved_by: str | None = None,
    ) -> PipelineExecution:
        """Approve a pipeline execution that is waiting for approval."""
        execution = await self.approval_manager.approve_step(token, approved_by)

        # Resume execution
        if self.loader:
            try:
                pipeline = await self.loader.load_pipeline(execution.pipeline_name)
                if pipeline:
                    inputs = {}
                    if execution.inputs_json:
                        try:
                            inputs = json.loads(execution.inputs_json)
                        except json.JSONDecodeError:
                            pass

                    # Execute (resume)
                    execution = await self.execute(
                        pipeline=pipeline,
                        inputs=inputs,
                        project_id=execution.project_id,
                        execution_id=execution.id,
                    )
            except ApprovalRequired:
                # Pipeline paused again for another approval - this is expected
                # Refresh execution to get latest status
                exec_id = execution.id  # Save before get_execution may return None
                refreshed = self.execution_manager.get_execution(exec_id)
                if not refreshed:
                    raise ValueError(f"Execution {exec_id} not found after resume") from None
                execution = refreshed
            except Exception as e:
                logger.error(f"Failed to resume execution after approval: {e}", exc_info=True)
                # Don't fail the approval if resume fails, but log it
        else:
            logger.warning("No loader configured, cannot resume execution automatically")

        return execution

    async def reject(
        self,
        token: str,
        rejected_by: str | None = None,
    ) -> PipelineExecution:
        """Reject a pipeline execution that is waiting for approval."""
        return await self.approval_manager.reject_step(token, rejected_by)

    async def _execute_nested_pipeline(
        self,
        pipeline_ref: str | dict[str, Any],
        context: dict[str, Any],
        project_id: str,
    ) -> dict[str, Any]:
        """Execute a nested pipeline.

        Args:
            pipeline_ref: Pipeline name (str) or dict with 'name' and optional 'arguments'
            context: Execution context (used as inputs)
            project_id: Project context

        Returns:
            Dict with nested pipeline outputs
        """
        # Parse dict-style invoke_pipeline
        if isinstance(pipeline_ref, dict):
            pipeline_name = pipeline_ref.get("name", "")
            explicit_args = pipeline_ref.get("arguments")
        else:
            pipeline_name = pipeline_ref
            explicit_args = None

        logger.info(f"Invoking nested pipeline: {pipeline_name}")

        # Check if loader is available
        if not self.loader:
            logger.warning("No loader configured for nested pipeline execution")
            return {
                "pipeline": pipeline_name,
                "error": "No loader configured for nested pipeline execution",
            }

        try:
            # Load the nested pipeline
            nested_pipeline = await self.loader.load_pipeline(pipeline_name)

            if not nested_pipeline:
                return {
                    "pipeline": pipeline_name,
                    "error": f"Pipeline '{pipeline_name}' not found",
                }

            # Use explicit arguments if provided, otherwise inherit parent inputs
            if explicit_args is not None:
                nested_inputs = explicit_args
            else:
                nested_inputs = context.get("inputs", {})

            # Propagate session_id and nesting state to nested execution
            parent_depth: int = context.get("_depth", 0)
            parent_stack: frozenset[str] = context.get("_pipeline_stack", frozenset())
            result = await self.execute(
                pipeline=nested_pipeline,
                inputs=nested_inputs,
                project_id=project_id,
                session_id=context.get("session_id"),
                _depth=parent_depth + 1,
                _pipeline_stack=parent_stack,
                _parent_session_id=context.get("parent_session_id"),
            )

            # Surface child pipeline outputs so parent steps can reference them
            # e.g. ${{ dev_loop.output.orchestration_complete }}
            step_output: dict[str, Any] = {
                "pipeline": pipeline_name,
                "execution_id": result.id,
                "status": result.status.value,
            }
            if result.outputs_json:
                try:
                    child_outputs = json.loads(result.outputs_json)
                    step_output["output"] = child_outputs
                except (json.JSONDecodeError, TypeError):
                    pass
            return step_output

        except Exception as e:
            logger.error(f"Nested pipeline execution failed: {e}", exc_info=True)
            return {
                "pipeline": pipeline_name,
                "error": str(e),
            }

    def _build_outputs(self, pipeline: Any, context: dict[str, Any]) -> dict[str, Any]:
        """Build pipeline outputs from context.

        Args:
            pipeline: The pipeline definition
            context: Final execution context

        Returns:
            Dict of output name -> value
        """
        outputs: dict[str, Any] = {}
        # Build render context once for Jinja2 expressions
        render_ctx = (
            self.renderer.build_render_context(context) if self.renderer.template_engine else {}
        )

        for name, expr in pipeline.outputs.items():
            if isinstance(expr, str) and "${{" in expr:
                # Pure expression: use SafeExpressionEvaluator (has len, bool, etc.)
                m = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", expr.strip(), re.DOTALL)
                if m:
                    resolved = self.renderer._resolve_expression(m.group(1), render_ctx)
                    outputs[name] = (
                        _coerce_rendered_value(resolved) if isinstance(resolved, str) else resolved
                    )
                else:
                    # Mixed string with embedded ${{ }}: use Jinja2
                    rendered = self.renderer.render_string(expr, render_ctx)
                    outputs[name] = _coerce_rendered_value(rendered)
            elif isinstance(expr, str) and expr.startswith("$"):
                # Resolve $step.output reference
                value = self._resolve_reference(expr, context)
                outputs[name] = value
            else:
                outputs[name] = expr

        return outputs

    def _resolve_reference(self, ref: str, context: dict[str, Any]) -> Any:
        """Resolve a $step.output reference from context."""
        return self.renderer.resolve_reference(ref, context)
