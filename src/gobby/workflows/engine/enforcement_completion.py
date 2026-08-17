"""Workflow completion and post-tool transition handling."""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent
from gobby.hooks.normalization import normalize_tool_fields
from gobby.hooks.tool_outcomes import tool_outcome_from_data
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_audit import WorkflowAuditManager
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.engine._offload import offload
from gobby.workflows.step_instances import (
    AgentStepInstanceManager,
    StaleStepInstanceWriteError,
)

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.workflows.step_instances import AgentStepInstance

logger = logging.getLogger("gobby.workflows.engine.enforcement")


def _facade_attr(name: str) -> Any:
    from gobby.workflows.engine import enforcement

    return getattr(enforcement, name)


class EnforcementCompletionMixin:
    """Complete agent workflows and process post-tool transitions."""

    db: HubDatabase
    instance_manager: AgentStepInstanceManager
    workflow_audit: WorkflowAuditManager

    if TYPE_CHECKING:
        _runner: AgentRunner | None
        _completion_registry: CompletionEventRegistry | None

        def _evaluate_condition(
            self, condition: str, ctx: dict[str, Any], effect_type: str
        ) -> bool: ...

        def _get_step_for_session(
            self, session_id: str
        ) -> tuple[WorkflowStep | None, AgentStepInstance | None]: ...

        def _is_native_set_variable_tool(self, tool_name: str) -> bool: ...

        def _step_handler_tool_input(self, tool_input: dict[str, Any]) -> dict[str, Any]: ...

        def _successful_set_variable_value(
            self, handler_tool_input: dict[str, Any], tool_output: Any
        ) -> tuple[str | None, Any]: ...

        def _evaluate_step_handler_value(
            self, value: Any, ctx: dict[str, Any], effect_type: str
        ) -> tuple[bool, Any]: ...

        def _audit_step_tool_call(
            self,
            session_id: str,
            workflow: str,
            step: str,
            tool_name: str,
            result: str,
            *,
            reason: str | None = None,
            mcp_key: str | None = None,
        ) -> None: ...

        def _audit_step_set_variable(
            self,
            session_id: str,
            workflow: str,
            step: str,
            mcp_key: str,
            variable: str,
            value: Any,
        ) -> None: ...

        def _step_audit_context(
            self,
            workflow: str,
            step: str,
            *,
            mcp_key: str | None = None,
            **extra: Any,
        ) -> dict[str, Any]: ...

    async def _complete_agent_workflow_run(
        self,
        session_id: str,
        workflow_name: str,
    ) -> None:
        """Complete an agent-backed run when its workflow reaches a terminal step."""

        if self._runner is None:
            return

        run_storage: LocalAgentRunManager | Any | None = getattr(self._runner, "run_storage", None)
        logger.debug(
            "_complete_agent_workflow_run session=%s workflow=%s run_storage=%s",
            session_id,
            workflow_name,
            type(run_storage).__name__ if run_storage is not None else None,
        )
        db_agent: Any | None = None
        get_by_session = getattr(run_storage, "get_by_session", None)
        logger.debug(
            "_complete_agent_workflow_run session=%s workflow=%s has_get_by_session=%s",
            session_id,
            workflow_name,
            callable(get_by_session),
        )
        if callable(get_by_session):
            db_agent = await offload(get_by_session, session_id)
            logger.debug(
                "_complete_agent_workflow_run session=%s workflow=%s db_agent=%s",
                session_id,
                workflow_name,
                getattr(db_agent, "id", None),
            )
        fallback_run_id: str | None = None
        if db_agent is None:
            fallback_run_id = await offload(
                self._runner.get_run_id_by_session,
                session_id,
            )
            logger.debug(
                "_complete_agent_workflow_run session=%s workflow=%s fallback_run_id=%s",
                session_id,
                workflow_name,
                fallback_run_id,
            )
        run_id = db_agent.id if db_agent else fallback_run_id
        if not run_id:
            logger.debug(
                "_complete_agent_workflow_run session=%s workflow=%s no_run_id_found",
                session_id,
                workflow_name,
            )
            return

        # get_by_session only returns running/pending runs, so a terminal run
        # (e.g. one parked by a daemon stop) arrives via the fallback lookup and
        # must be re-read to learn its actual terminal_reason.
        run_row: Any | None = db_agent
        if run_row is None:
            run_row = await offload(self._runner.get_run, run_id)
        terminal_reason: str | None = getattr(run_row, "terminal_reason", None)

        notify_result: dict[str, Any] = {
            "status": "success",
            "run_id": run_id,
            "via": "workflow_terminate",
            "workflow": workflow_name,
        }
        message = f"Agent {run_id} completed via workflow terminate"

        lifecycle_monitor = getattr(self._runner, "agent_lifecycle_monitor", None)
        terminalize_successful_run: Any = getattr(
            lifecycle_monitor,
            "terminalize_successful_run",
            None,
        )
        cleanup_session_id: str | None = (
            getattr(db_agent, "child_session_id", None) if db_agent else None
        )
        if not isinstance(cleanup_session_id, str) or not cleanup_session_id:
            cleanup_session_id = session_id
        cleanup_agent_runtime_state = _facade_attr("cleanup_agent_runtime_state")
        if terminal_reason == "daemon_stop":
            # The run was parked by a daemon stop: its agent_step_instances
            # are retained for resume and waiting subscribers must not receive
            # a false success. Forward the parked reason so cleanup keeps the
            # workflow rows, and skip completion delivery entirely.
            logger.debug(
                "_complete_agent_workflow_run session=%s workflow=%s run=%s "
                "suppressed for daemon_stop parked run",
                session_id,
                workflow_name,
                run_id,
            )
            await offload(
                cleanup_agent_runtime_state,
                self.db,
                run_id=run_id,
                child_session_id=cleanup_session_id,
                terminal_reason=terminal_reason,
            )
            return
        # Lifecycle monitor terminalizers are async by contract. A sync callable
        # is treated as unavailable so workflow completion uses the runner path.
        if inspect.iscoroutinefunction(terminalize_successful_run):
            terminalized = await terminalize_successful_run(
                run_id,
                notify_result=notify_result,
                message=message,
            )
            logger.debug(
                "Workflow lifecycle terminalization settled acknowledged delivery for %s: %s",
                run_id,
                terminalized,
            )
            await offload(
                cleanup_agent_runtime_state,
                self.db,
                run_id=run_id,
                child_session_id=cleanup_session_id,
                terminal_reason=terminal_reason,
            )
            return
        if callable(terminalize_successful_run):
            logger.warning(
                "Ignoring synchronous terminalize_successful_run hook for run %s",
                run_id,
            )

        complete_and_notify_agent_run = _facade_attr("complete_and_notify_agent_run")
        await complete_and_notify_agent_run(
            self._runner,
            run_id,
            completion_registry=self._completion_registry,
            notify_result=notify_result,
            message=message,
        )
        await offload(
            cleanup_agent_runtime_state,
            self.db,
            run_id=run_id,
            child_session_id=cleanup_session_id,
            terminal_reason=terminal_reason,
        )

    async def _process_step_after_tool(
        self, event: HookEvent, session_id: str, variables: dict[str, Any]
    ) -> str | None:
        """Process step workflow on_mcp_success handlers and transitions after tool completion.

        Returns:
            A transition notification string if a step transition occurred,
            or None if no transition happened. The notification includes the
            new step's status_message for injection into AfterTool additionalContext.
        """
        step, instance = await offload(
            self._get_step_for_session,
            session_id,
        )
        if step is None or instance is None:
            return None
        definition = instance.snapshot
        cas_token = (str(instance.id), instance.updated_at)

        tool_name = event.data.get("tool_name", "")
        tool_input = event.data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        is_native_set_variable = self._is_native_set_variable_tool(tool_name)
        if tool_name in ("call_tool", "mcp__gobby__call_tool", "mcp_gobby_call_tool"):
            mcp_server = tool_input.get("server_name", "")
            mcp_tool_name = tool_input.get("tool_name", "")
            if not mcp_server or not mcp_tool_name:
                return None
            mcp_key = f"{mcp_server}:{mcp_tool_name}"
        elif is_native_set_variable:
            mcp_server = "gobby-workflows"
            mcp_tool_name = "set_variable"
            mcp_key = f"{mcp_server}:{mcp_tool_name}"
        else:
            return None

        outcome = tool_outcome_from_data(event.data)
        if outcome.succeeded is None:
            normalize_tool_fields(event.data)
            outcome = tool_outcome_from_data(event.data)
        tool_output = event.data.get("tool_output")
        tool_failed = bool(event.metadata.get("is_failure", False)) or outcome.succeeded is False
        if not tool_failed:
            await offload(
                self._audit_step_tool_call,
                session_id,
                instance.agent_name,
                step.name,
                tool_name,
                "allow",
                reason=f"MCP tool '{mcp_key}' completed successfully",
                mcp_key=mcp_key,
            )

        handlers = step.on_mcp_error if tool_failed else step.on_mcp_success
        handler_tool_input = self._step_handler_tool_input(tool_input)

        instance_mgr = self.instance_manager
        vars_changed = False

        if is_native_set_variable and not tool_failed:
            var_name, var_value = self._successful_set_variable_value(
                handler_tool_input,
                tool_output,
            )
            if var_name and var_name not in instance.variables:
                variables[var_name] = var_value
                vars_changed = True

        # Execute handlers (on_mcp_success or on_mcp_error based on tool output)
        for handler in handlers:
            if handler.get("server") == mcp_server and handler.get("tool") == mcp_tool_name:
                handler_when = handler.get("when")
                if handler_when:
                    handler_matches = await offload(
                        self._evaluate_condition,
                        handler_when,
                        {
                            # Instance variables last so workflow-local state wins
                            # over session-wide observer/handoff state with
                            # colliding names (e.g. task_claimed).
                            "vars": {**variables, **instance.variables},
                            "tool_input": handler_tool_input,
                            "tool_output": tool_output,
                        },
                        str(handler.get("action") or "set_variable"),
                    )
                    if not handler_matches:
                        continue
                if handler.get("action") == "set_variable":
                    var_name = handler.get("variable")
                    ctx = {
                        # Instance variables last so workflow-local state wins
                        # over session-wide observer/handoff state with
                        # colliding names (e.g. task_claimed).
                        "vars": {**variables, **instance.variables},
                        "variables": {**variables, **instance.variables},
                        "tool_input": handler_tool_input,
                        "tool_output": tool_output,
                    }
                    ok, var_value = await offload(
                        self._evaluate_step_handler_value,
                        handler.get("value"),
                        ctx,
                        str(handler.get("action") or "set_variable"),
                    )
                    if var_name is not None and ok:
                        instance.variables[var_name] = var_value
                        variables[var_name] = var_value
                        vars_changed = True
                        await offload(
                            self._audit_step_set_variable,
                            session_id,
                            instance.agent_name,
                            step.name,
                            mcp_key,
                            str(var_name),
                            var_value,
                        )

        # Skip transitions when tool failed and no error handlers modified state
        if tool_failed and not vars_changed:
            return None

        transition_steps: list[tuple[str, str]] = []
        current_step_def = step
        max_transitions = len(definition.steps) + 1

        for _ in range(max_transitions):
            transition_taken = False

            for transition in current_step_def.transitions:
                # Instance variables last so workflow-local state wins over
                # session-wide observer/handoff state with colliding names.
                # Without this precedence, a session-level task_claimed=True
                # written by _session_start task handoff would fire the
                # claim -> load_skill transition before the workflow's own
                # claim_task handler ever runs (see task #12267).
                ctx = {"vars": {**variables, **instance.variables}}
                transition_met = not transition.when or await offload(
                    self._evaluate_condition,
                    transition.when,
                    ctx,
                    "transition",
                )
                if not transition_met:
                    continue

                old_step = instance.current_step or ""
                new_step = transition.to
                new_step_def = definition.get_step(new_step)

                if not new_step_def:
                    logger.warning(
                        "Transition to unknown step '%s' in workflow '%s'",
                        new_step,
                        instance.agent_name,
                    )
                    continue

                await offload(
                    self.workflow_audit.log_transition,
                    session_id=session_id,
                    from_step=old_step,
                    to_step=new_step,
                    reason="Transition condition met",
                    context=self._step_audit_context(
                        instance.agent_name,
                        old_step,
                        condition=transition.when,
                        result=True,
                        to_step=new_step,
                    ),
                )

                instance.current_step = new_step
                instance.step_action_count = 0
                instance.step_entered_at = datetime.now(UTC)
                try:
                    await offload(
                        instance_mgr.save,
                        instance,
                        if_match=cas_token,
                    )
                except StaleStepInstanceWriteError:
                    return None
                cas_token = (str(instance.id), instance.updated_at)

                # Reset consecutive-tool-block counters so failures from the
                # previous step don't bleed into the new one
                variables["consecutive_tool_blocks"] = 0
                variables["_last_blocked_tool"] = ""
                variables["tool_block_pending"] = False

                logger.info(
                    "Step transition: %s -> %s (workflow=%s, session=%s)",
                    old_step,
                    new_step,
                    instance.agent_name,
                    session_id,
                )

                transition_steps.append((old_step, new_step))
                current_step_def = new_step_def
                transition_taken = True

                # Evaluate exit_condition after transition
                if definition.exit_condition:
                    merged_vars = {**variables, **instance.variables}
                    exit_ctx = {
                        "current_step": instance.current_step,
                        "vars": merged_vars,
                        "variables": merged_vars,
                    }
                    exit_met = await offload(
                        self._evaluate_condition,
                        definition.exit_condition,
                        exit_ctx,
                        "block",
                    )
                    await offload(
                        self.workflow_audit.log_exit_check,
                        session_id=session_id,
                        step=instance.current_step,
                        condition=definition.exit_condition,
                        result="met" if exit_met else "unmet",
                        reason=("Exit condition met" if exit_met else "Exit condition was not met"),
                        context=self._step_audit_context(
                            instance.agent_name,
                            instance.current_step,
                            condition=definition.exit_condition,
                            result=exit_met,
                        ),
                    )
                    if exit_met:
                        variables["step_workflow_complete"] = True
                        logger.info(
                            "Exit condition met for workflow %s (session=%s, step=%s)",
                            instance.agent_name,
                            session_id,
                            instance.current_step,
                        )
                        await self._complete_agent_workflow_run(
                            session_id,
                            instance.agent_name,
                        )

                break

            if not transition_taken:
                break
        else:
            logger.warning(
                "Stopped step transition chain for workflow %s (session=%s) after %d transitions",
                instance.agent_name,
                session_id,
                max_transitions,
            )

        if transition_steps:
            path = [transition_steps[0][0], *(to_step for _, to_step in transition_steps)]
            transition_notice = f"Step transition: {' -> '.join(path)}"
            status_msg = (current_step_def.status_message or "").strip()
            if status_msg:
                transition_notice += f"\n{status_msg}"
            return transition_notice

        # Save if variables changed without transition
        if vars_changed:
            try:
                await offload(instance_mgr.save, instance, if_match=cas_token)
            except StaleStepInstanceWriteError:
                return None

        return None
