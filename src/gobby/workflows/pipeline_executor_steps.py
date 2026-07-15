"""Step, approval, and nested-pipeline helpers for PipelineExecutor."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, cast

from opentelemetry.trace import Status, StatusCode

from gobby.telemetry.tracing import create_span
from gobby.workflows.pipeline_state import ApprovalRequired, ExecutionStatus, PipelineExecution

logger = logging.getLogger("gobby.workflows.pipeline_executor")
_FACADE_MODULE = "gobby.workflows.pipeline_executor"


def _facade_attr(name: str) -> Any:
    return getattr(sys.modules[_FACADE_MODULE], name)


class PipelineExecutorStepMixin:
    """Step dispatch, approval resume, wait, and nested-pipeline helpers."""

    approval_manager: Any
    completion_registry: Any
    execution_manager: Any
    llm_service: Any
    loader: Any
    pipeline_config: Any
    renderer: Any
    session_manager: Any
    tool_proxy_getter: Any

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

                # Warn if multiple step types are set - only the first match executes
                if len(step_types) > 1:
                    logger.warning(
                        f"Step {step.id} has multiple types set: {step_types} - only '{step_types[0]}' will execute",
                    )

                if step.wait:
                    # Block until completion event fires
                    return await self._execute_wait_step(rendered_step, context)
                elif step.exec:
                    # Execute shell command
                    return await _facade_attr("execute_exec_step")(rendered_step.exec, context)
                elif step.prompt:
                    # Execute LLM prompt
                    return await _facade_attr("execute_prompt_step")(
                        rendered_step.prompt,
                        context,
                        self.llm_service,
                        self.pipeline_config.prompt_step,
                    )
                elif step.invoke_pipeline:
                    # Execute nested pipeline
                    return await self._execute_nested_pipeline(
                        rendered_step.invoke_pipeline, context, project_id
                    )
                elif step.mcp:
                    # Execute MCP tool call
                    return await _facade_attr("execute_mcp_step")(
                        rendered_step,
                        context,
                        self.tool_proxy_getter,
                        self.session_manager,
                    )
                elif step.activate_workflow:
                    raise RuntimeError("activate_workflow pipeline steps are not supported")
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
        execution = cast(
            PipelineExecution,
            await self.approval_manager.approve_step(token, approved_by),
        )

        # Resume execution
        if self.loader:
            pipeline = None
            try:
                pipeline = await self.loader.load_pipeline(
                    execution.pipeline_name, execution.project_id
                )
                if pipeline:
                    if not pipeline.enabled:
                        self.execution_manager.update_execution_status(
                            execution_id=execution.id,
                            status=ExecutionStatus.CANCELLED,
                        )
                        raise ValueError(f"Pipeline '{pipeline.name}' is disabled")

                    inputs = {}
                    if execution.inputs_json:
                        try:
                            inputs = json.loads(execution.inputs_json)
                        except json.JSONDecodeError:
                            pass

                    # Execute (resume)
                    execution = cast(
                        PipelineExecution,
                        await cast(Any, self).execute(
                            pipeline=pipeline,
                            inputs=inputs,
                            project_id=execution.project_id,
                            execution_id=execution.id,
                        ),
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
                if pipeline is not None and not pipeline.enabled:
                    raise
                logger.error(f"Failed to resume execution after approval: {e}", exc_info=True)
                refreshed = self.execution_manager.get_execution(execution.id)
                if refreshed:
                    execution = refreshed
                # Don't fail the approval if resume fails, but return refreshed state if available.
        else:
            logger.warning("No loader configured, cannot resume execution automatically")

        return execution

    async def reject(
        self,
        token: str,
        rejected_by: str | None = None,
    ) -> PipelineExecution:
        """Reject a pipeline execution that is waiting for approval."""
        return cast(
            PipelineExecution,
            await self.approval_manager.reject_step(token, rejected_by),
        )

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

        if not self.loader:
            raise RuntimeError("No loader configured for nested pipeline execution")

        try:
            # Load the nested pipeline
            nested_pipeline = await self.loader.load_pipeline(pipeline_name, project_id)

            if not nested_pipeline:
                raise RuntimeError(f"Pipeline '{pipeline_name}' not found")

            # Use explicit arguments if provided, otherwise inherit parent inputs
            if explicit_args is not None:
                nested_inputs = explicit_args
            else:
                nested_inputs = context.get("inputs", {})

            # Propagate session_id and nesting state to nested execution
            parent_depth: int = context.get("_depth", 0)
            parent_stack: frozenset[str] = context.get("_pipeline_stack", frozenset())
            result = await cast(Any, self).execute(
                pipeline=nested_pipeline,
                inputs=nested_inputs,
                project_id=project_id,
                session_id=context.get("session_id"),
                _depth=parent_depth + 1,
                _pipeline_stack=parent_stack,
                _parent_session_id=context.get("parent_session_id"),
            )
            if result.status.value != "completed":
                raise RuntimeError(
                    f"Nested pipeline '{pipeline_name}' finished with status {result.status.value}"
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

        except ApprovalRequired:
            raise
        except Exception as e:
            logger.error(f"Nested pipeline execution failed: {e}", exc_info=True)
            return {
                "pipeline": pipeline_name,
                "error": str(e),
            }
