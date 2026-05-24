"""Agent and step tool enforcement for the rule engine.

Handles tool allow/block lists at the agent and step workflow levels,
MCP tool matching, and step workflow transition processing.
"""

import inspect
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pydantic

from gobby.agents.run_completion import complete_and_notify_agent_run
from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state
from gobby.hooks.events import HookEvent, HookResponse
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_audit import WorkflowAuditManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowDefinition, WorkflowStep
from gobby.workflows.enforcement.blocking import (
    is_discovery_tool,
    is_infrastructure_tool,
    is_operator_tool,
)
from gobby.workflows.engine.skill_load_guidance import skill_load_block_guidance
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.state_manager import WorkflowInstanceManager

logger = logging.getLogger(__name__)

RESERVED_STEP_WORKFLOW_VARIABLES = frozenset({"step_workflow_complete", "_step_workflow_name"})


class EnforcementMixin:
    """Mixin providing tool enforcement methods for RuleEngine."""

    db: HubDatabase
    instance_manager: WorkflowInstanceManager
    definition_manager: LocalWorkflowDefinitionManager
    workflow_audit: WorkflowAuditManager

    if TYPE_CHECKING:
        from gobby.agents.runner import AgentRunner
        from gobby.events.completion_registry import CompletionEventRegistry

        # Provided by TemplatingMixin at runtime via RuleEngine MRO
        def _evaluate_condition(
            self, condition: str, ctx: dict[str, Any], effect_type: str
        ) -> bool: ...

        _runner: AgentRunner | None
        _completion_registry: CompletionEventRegistry | None

    @staticmethod
    def _audit_value(value: Any) -> Any:
        try:
            json.dumps(value)
            return value
        except TypeError:
            return repr(value)

    @staticmethod
    def _step_audit_context(
        workflow: str,
        step: str,
        *,
        mcp_key: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        context = {"workflow": workflow, "step": step}
        if mcp_key:
            context["mcp_key"] = mcp_key
        for key, value in extra.items():
            if value is not None:
                context[key] = EnforcementMixin._audit_value(value)
        return context

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
    ) -> None:
        self.workflow_audit.log_tool_call(
            session_id=session_id,
            step=step,
            tool_name=mcp_key or tool_name,
            result=result,
            reason=reason,
            context=self._step_audit_context(workflow, step, mcp_key=mcp_key),
        )

    def _audit_step_set_variable(
        self,
        session_id: str,
        workflow: str,
        step: str,
        mcp_key: str,
        variable: str,
        value: Any,
    ) -> None:
        self.workflow_audit.log(
            session_id=session_id,
            step=step,
            event_type="set_variable",
            result="set",
            reason=f"Set workflow variable '{variable}'",
            context=self._step_audit_context(
                workflow,
                step,
                mcp_key=mcp_key,
                variable=variable,
                value=value,
            ),
        )

    def _get_step_for_session(
        self, session_id: str
    ) -> tuple[WorkflowStep | None, Any | None, WorkflowDefinition | None]:
        """Get the current workflow step, instance, and definition for a session.

        Returns (step, instance, definition) or (None, None, None) if no active step workflow.
        """
        if not session_id:
            return None, None, None
        instances = self.instance_manager.get_active_instances(session_id)

        for instance in instances:
            if not instance.current_step:
                continue
            row = self.definition_manager.get_by_name(instance.workflow_name)
            if not row or row.workflow_type == "pipeline":
                continue
            try:
                data = json.loads(row.definition_json)
                definition = WorkflowDefinition(**data)
            except (json.JSONDecodeError, pydantic.ValidationError) as e:
                logger.warning(
                    f"Skipping malformed workflow definition '{instance.workflow_name}': {e}",
                )
                continue
            step = definition.get_step(instance.current_step)
            if step is not None:
                return step, instance, definition
        return None, None, None

    def _check_agent_tool_enforcement(
        self, event: HookEvent, session_id: str, variables: dict[str, Any]
    ) -> HookResponse | None:
        """Check agent-level tool restrictions. Returns block response or None to continue."""
        blocked_tools: list[str] = variables.get("_agent_blocked_tools") or []
        blocked_mcp_tools: list[str] = variables.get("_agent_blocked_mcp_tools") or []
        if not blocked_tools and not blocked_mcp_tools:
            return None

        tool_name = event.data.get("tool_name", "")
        agent_type = variables.get("_agent_type", "unknown")

        # Discovery/infrastructure tools always pass
        if tool_name.startswith("mcp__gobby__"):
            mcp_suffix = tool_name[len("mcp__gobby__") :]
            if is_discovery_tool(mcp_suffix) or is_infrastructure_tool(mcp_suffix):
                return None

        # Check native tool block-list
        if blocked_tools and tool_name in blocked_tools:
            return HookResponse(
                decision="block",
                reason=(
                    f"Rule enforced by Gobby: [agent-enforcement:{agent_type}]\n"
                    f"Tool '{tool_name}' is blocked for the '{agent_type}' agent."
                ),
            )

        # Check MCP tool restrictions (for call_tool)
        if blocked_mcp_tools and tool_name in (
            "call_tool",
            "mcp__gobby__call_tool",
            "mcp_gobby_call_tool",
        ):
            tool_input = event.data.get("tool_input") or {}
            if isinstance(tool_input, dict):
                mcp_server = tool_input.get("server_name", "")
                mcp_tool_name = tool_input.get("tool_name", "")

                # Discovery MCP tools always pass
                if is_discovery_tool(mcp_tool_name):
                    return None

                mcp_key = f"{mcp_server}:{mcp_tool_name}" if mcp_server and mcp_tool_name else ""

                # Operator/debug MCP tools bypass agent block-lists
                if is_operator_tool(mcp_tool_name):
                    return None

                if mcp_key and self._mcp_tool_matches(mcp_key, blocked_mcp_tools):
                    return HookResponse(
                        decision="block",
                        reason=(
                            f"Rule enforced by Gobby: [agent-enforcement:{agent_type}]\n"
                            f"MCP tool '{mcp_key}' is blocked for the '{agent_type}' agent."
                        ),
                    )

        return None

    def _check_step_tool_enforcement(
        self, event: HookEvent, session_id: str, variables: dict[str, Any]
    ) -> HookResponse | None:
        """Check step-level tool restrictions. Returns block response or None to continue."""
        step, instance, _defn = self._get_step_for_session(session_id)
        if step is None or instance is None:
            return None

        tool_name = event.data.get("tool_name", "")
        wf_name = instance.workflow_name

        # ToolSearch (Claude Code deferred tool loader) is always allowed
        if tool_name == "ToolSearch":
            return None

        if tool_name in ("set_variable", "mcp__gobby__set_variable", "mcp_gobby_set_variable"):
            tool_input = event.data.get("tool_input") or {}
            variable_name = ""
            if isinstance(tool_input, dict):
                variable_name = self._is_reserved_variable_write(tool_name, tool_input) or ""
            if variable_name:
                reason = (
                    f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                    f"Variable '{variable_name}' is managed by the step workflow runtime."
                )
                self._audit_step_tool_call(
                    session_id,
                    wf_name,
                    step.name,
                    tool_name,
                    "block",
                    reason=reason,
                )
                return HookResponse(decision="block", reason=reason)

        # Discovery/infrastructure tools always pass
        if tool_name.startswith("mcp__gobby__"):
            mcp_suffix = tool_name[len("mcp__gobby__") :]
            if is_discovery_tool(mcp_suffix) or is_infrastructure_tool(mcp_suffix):
                return None

        # Check native tool allow-list
        if step.allowed_tools != "all":
            if tool_name not in step.allowed_tools:
                guidance = skill_load_block_guidance(step)
                reason = (
                    f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                    f"Tool '{tool_name}' is not allowed in the '{step.name}' step.\n"
                    f"Allowed tools: {', '.join(step.allowed_tools)}{guidance}"
                )
                self._audit_step_tool_call(
                    session_id,
                    wf_name,
                    step.name,
                    tool_name,
                    "block",
                    reason=reason,
                )
                return HookResponse(
                    decision="block",
                    reason=reason,
                )

        # Check native tool block-list
        if tool_name in step.blocked_tools:
            reason = (
                f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                f"Tool '{tool_name}' is blocked in the '{step.name}' step."
            )
            self._audit_step_tool_call(
                session_id,
                wf_name,
                step.name,
                tool_name,
                "block",
                reason=reason,
            )
            return HookResponse(
                decision="block",
                reason=reason,
            )

        # Check MCP tool restrictions (for call_tool)
        if tool_name in ("call_tool", "mcp__gobby__call_tool", "mcp_gobby_call_tool"):
            tool_input = event.data.get("tool_input") or {}
            if isinstance(tool_input, dict):
                mcp_server = tool_input.get("server_name", "")
                mcp_tool_name = tool_input.get("tool_name", "")

                # Discovery MCP tools always pass
                if is_discovery_tool(mcp_tool_name):
                    return None

                # Operator/debug MCP tools (e.g. send_keys) always pass so
                # humans can drive a stuck agent regardless of its step
                # allow-list
                if is_operator_tool(mcp_tool_name):
                    return None

                mcp_key = f"{mcp_server}:{mcp_tool_name}" if mcp_server and mcp_tool_name else ""

                if mcp_key and step.allowed_mcp_tools != "all":
                    if not self._mcp_tool_matches(mcp_key, step.allowed_mcp_tools):
                        guidance = skill_load_block_guidance(step)
                        reason = (
                            f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                            f"MCP tool '{mcp_key}' is not allowed in the '{step.name}' step.\n"
                            f"Allowed MCP tools: {', '.join(step.allowed_mcp_tools)}{guidance}"
                        )
                        self._audit_step_tool_call(
                            session_id,
                            wf_name,
                            step.name,
                            tool_name,
                            "block",
                            reason=reason,
                            mcp_key=mcp_key,
                        )
                        return HookResponse(
                            decision="block",
                            reason=reason,
                        )

                if mcp_key and step.blocked_mcp_tools:
                    if self._mcp_tool_matches(mcp_key, step.blocked_mcp_tools):
                        reason = (
                            f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                            f"MCP tool '{mcp_key}' is blocked in the '{step.name}' step."
                        )
                        self._audit_step_tool_call(
                            session_id,
                            wf_name,
                            step.name,
                            tool_name,
                            "block",
                            reason=reason,
                            mcp_key=mcp_key,
                        )
                        return HookResponse(
                            decision="block",
                            reason=reason,
                        )

                if mcp_tool_name == "set_variable":
                    variable_name = (
                        self._is_reserved_variable_write(
                            tool_name,
                            tool_input,
                            mcp_tool_name=mcp_tool_name,
                        )
                        or ""
                    )
                    if variable_name:
                        reason = (
                            f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                            f"Variable '{variable_name}' is managed by the step workflow runtime."
                        )
                        self._audit_step_tool_call(
                            session_id,
                            wf_name,
                            step.name,
                            tool_name,
                            "block",
                            reason=reason,
                            mcp_key=mcp_key,
                        )
                        return HookResponse(decision="block", reason=reason)

                if mcp_key:
                    if event.metadata.get("_mcp_proxy_duplicate_before_tool") is True:
                        return None
                    handler_tool_input = self._step_handler_tool_input(tool_input)
                    before_response = self._process_step_before_mcp_tool(
                        event,
                        session_id,
                        variables,
                        step,
                        instance,
                        tool_name,
                        mcp_server,
                        mcp_tool_name,
                        mcp_key,
                        handler_tool_input,
                    )
                    if before_response is not None:
                        return before_response

        return None

    @staticmethod
    def _is_reserved_variable_write(
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        mcp_tool_name: str | None = None,
    ) -> str | None:
        """Return the reserved variable name for blocked user writes."""
        is_native_set_variable = tool_name in (
            "set_variable",
            "mcp__gobby__set_variable",
            "mcp_gobby_set_variable",
        )
        is_mcp_set_variable = mcp_tool_name == "set_variable"
        if not is_native_set_variable and not is_mcp_set_variable:
            return None

        resolved_input = (
            EnforcementMixin._step_handler_tool_input(tool_input)
            if is_mcp_set_variable
            else tool_input
        )
        variable_name = str(resolved_input.get("name") or resolved_input.get("variable") or "")
        if variable_name in RESERVED_STEP_WORKFLOW_VARIABLES:
            return variable_name
        return None

    @staticmethod
    def _mcp_tool_matches(mcp_key: str, patterns: list[str]) -> bool:
        """Check if an MCP tool key (server:tool) matches any pattern in the list."""
        for pattern in patterns:
            if pattern == mcp_key:
                return True
            # Wildcard: "server:*"
            if pattern.endswith(":*") and mcp_key.startswith(pattern[:-1]):
                return True
        return False

    @staticmethod
    def _step_handler_tool_input(tool_input: dict[str, Any]) -> dict[str, Any]:
        """Return handler input with nested MCP arguments promoted for conditions."""
        handler_tool_input = dict(tool_input)
        raw_handler_args = tool_input.get("arguments", tool_input.get("args"))
        if isinstance(raw_handler_args, str):
            try:
                raw_handler_args = json.loads(raw_handler_args)
            except (json.JSONDecodeError, TypeError):
                raw_handler_args = None
        if isinstance(raw_handler_args, dict):
            handler_tool_input = {**raw_handler_args, **handler_tool_input}
        return handler_tool_input

    @staticmethod
    def _is_native_set_variable_tool(tool_name: str) -> bool:
        """Return whether a top-level set_variable tool name was called."""
        return tool_name in ("set_variable", "mcp__gobby__set_variable", "mcp_gobby_set_variable")

    @staticmethod
    def _successful_set_variable_value(
        handler_tool_input: dict[str, Any],
        tool_output: Any,
    ) -> tuple[str | None, Any]:
        """Return the set_variable name/value pair visible after a successful call."""
        variable_name = handler_tool_input.get("name") or handler_tool_input.get("variable")
        if not variable_name:
            return None, None

        if isinstance(tool_output, dict):
            if "value" in tool_output:
                return str(variable_name), tool_output["value"]
            result = tool_output.get("result")
            if isinstance(result, dict) and "value" in result:
                return str(variable_name), result["value"]

        return str(variable_name), handler_tool_input.get("value")

    @staticmethod
    def _is_step_handler_expression(value: str) -> bool:
        """Return whether a step handler value should be evaluated as an expression."""
        expression_indicators = (
            "vars.",
            "variables.",
            "tool_input.",
            "tool_output.",
            " + ",
            " - ",
            " and ",
            " or ",
            " not ",
            ".get(",
            "len(",
            "int(",
            "str(",
            "bool(",
        )
        return any(indicator in value for indicator in expression_indicators)

    def _evaluate_step_handler_value(
        self,
        value: Any,
        ctx: dict[str, Any],
        effect_type: str,
    ) -> tuple[bool, Any]:
        """Evaluate expression-like handler values using the workflow-safe evaluator."""
        if not isinstance(value, str) or not self._is_step_handler_expression(value):
            return True, value
        try:
            evaluator = SafeExpressionEvaluator(
                context=ctx,
                allowed_funcs={
                    "len": len,
                    "str": str,
                    "int": int,
                    "bool": bool,
                    "list": list,
                    "dict": dict,
                    "any": any,
                    "all": all,
                },
            )
            return True, evaluator.evaluate_value(value)
        except Exception as exc:
            logger.warning(
                "Failed to evaluate step %s handler value %r: %s", effect_type, value, exc
            )
            return False, None

    def _process_step_before_mcp_tool(
        self,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
        step: WorkflowStep,
        instance: Any,
        tool_name: str,
        mcp_server: str,
        mcp_tool_name: str,
        mcp_key: str,
        handler_tool_input: dict[str, Any],
    ) -> HookResponse | None:
        """Process step workflow on_mcp_before handlers for an allowed MCP call."""
        _ = event
        instance_mgr = self.instance_manager
        vars_changed = False

        for handler in step.on_mcp_before:
            if handler.get("server") != mcp_server or handler.get("tool") != mcp_tool_name:
                continue

            merged_vars = {**variables, **instance.variables}
            ctx = {
                "vars": merged_vars,
                "variables": merged_vars,
                "tool_input": handler_tool_input,
            }
            action = str(handler.get("action") or "set_variable")
            handler_when = handler.get("when")
            if handler_when and not self._evaluate_condition(handler_when, ctx, action):
                continue

            if action == "set_variable":
                var_name = handler.get("variable")
                ok, var_value = self._evaluate_step_handler_value(handler.get("value"), ctx, action)
                if var_name is not None and ok:
                    instance.variables[var_name] = var_value
                    variables[var_name] = var_value
                    vars_changed = True
                    self._audit_step_set_variable(
                        session_id,
                        instance.workflow_name,
                        step.name,
                        mcp_key,
                        str(var_name),
                        var_value,
                    )
                continue

            if action == "block":
                if vars_changed:
                    instance_mgr.save_instance(instance)
                raw_reason = str(
                    handler.get("reason")
                    or (f"MCP tool '{mcp_key}' is blocked by a workflow on_mcp_before handler.")
                )
                reason = (
                    f"Rule enforced by Gobby: "
                    f"[step-enforcement:{instance.workflow_name}/{step.name}]\n"
                    f"{raw_reason}"
                )
                self._audit_step_tool_call(
                    session_id,
                    instance.workflow_name,
                    step.name,
                    tool_name,
                    "block",
                    reason=reason,
                    mcp_key=mcp_key,
                )
                return HookResponse(decision="block", reason=reason)

        if vars_changed:
            instance_mgr.save_instance(instance)
        return None

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
            db_agent = get_by_session(session_id)
            logger.debug(
                "_complete_agent_workflow_run session=%s workflow=%s db_agent=%s",
                session_id,
                workflow_name,
                getattr(db_agent, "id", None),
            )
        fallback_run_id: str | None = None
        if db_agent is None:
            fallback_run_id = self._runner.get_run_id_by_session(session_id)
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
        # Lifecycle monitor terminalizers are async by contract. A sync callable
        # is treated as unavailable so workflow completion uses the runner path.
        if inspect.iscoroutinefunction(terminalize_successful_run):
            await terminalize_successful_run(
                run_id,
                notify_result=notify_result,
                message=message,
            )
            cleanup_agent_runtime_state(
                self.db,
                run_id=run_id,
                child_session_id=cleanup_session_id,
            )
            return
        if callable(terminalize_successful_run):
            logger.warning(
                "Ignoring synchronous terminalize_successful_run hook for run %s",
                run_id,
            )

        await complete_and_notify_agent_run(
            self._runner,
            run_id,
            completion_registry=self._completion_registry,
            notify_result=notify_result,
            message=message,
        )
        cleanup_agent_runtime_state(
            self.db,
            run_id=run_id,
            child_session_id=cleanup_session_id,
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
        step, instance, definition = self._get_step_for_session(session_id)
        if step is None or instance is None or definition is None:
            return None

        # Only process successful MCP tool completions
        is_failure = event.metadata.get("is_failure", False) or event.data.get("is_error", False)
        if is_failure:
            return None

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

        # Check application-level failure in tool output
        tool_output = event.data.get("tool_output")
        if isinstance(tool_output, str):
            try:
                tool_output = json.loads(tool_output)
            except (json.JSONDecodeError, TypeError):
                tool_output = None

        is_app_failure = False
        if isinstance(tool_output, dict):
            if tool_output.get("success") is False or bool(tool_output.get("error")):
                is_app_failure = True
            elif isinstance(tool_output.get("result"), dict):
                result_dict = tool_output["result"]
                if result_dict.get("success") is False or bool(result_dict.get("error")):
                    is_app_failure = True
        if not is_app_failure:
            self._audit_step_tool_call(
                session_id,
                instance.workflow_name,
                step.name,
                tool_name,
                "allow",
                reason=f"MCP tool '{mcp_key}' completed successfully",
                mcp_key=mcp_key,
            )

        handlers = step.on_mcp_error if is_app_failure else step.on_mcp_success
        handler_tool_input = dict(tool_input)
        raw_handler_args = tool_input.get("arguments", tool_input.get("args"))
        if isinstance(raw_handler_args, str):
            try:
                raw_handler_args = json.loads(raw_handler_args)
            except (json.JSONDecodeError, TypeError):
                raw_handler_args = None
        if isinstance(raw_handler_args, dict):
            handler_tool_input = {**raw_handler_args, **handler_tool_input}

        instance_mgr = self.instance_manager
        vars_changed = False

        if is_native_set_variable and not is_app_failure:
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
                if handler_when and not self._evaluate_condition(
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
                ):
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
                    ok, var_value = self._evaluate_step_handler_value(
                        handler.get("value"), ctx, str(handler.get("action") or "set_variable")
                    )
                    if var_name is not None and ok:
                        instance.variables[var_name] = var_value
                        variables[var_name] = var_value
                        vars_changed = True
                        self._audit_step_set_variable(
                            session_id,
                            instance.workflow_name,
                            step.name,
                            mcp_key,
                            str(var_name),
                            var_value,
                        )

        # Skip transitions when tool failed and no error handlers modified state
        if is_app_failure and not vars_changed:
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
                transition_met = not transition.when or self._evaluate_condition(
                    transition.when, ctx, "transition"
                )
                if not transition_met:
                    continue

                old_step = instance.current_step
                new_step = transition.to
                new_step_def = definition.get_step(new_step)

                if not new_step_def:
                    logger.warning(
                        f"Transition to unknown step '{new_step}' in workflow '{instance.workflow_name}'",
                    )
                    continue

                self.workflow_audit.log_transition(
                    session_id=session_id,
                    from_step=old_step,
                    to_step=new_step,
                    reason="Transition condition met",
                    context=self._step_audit_context(
                        instance.workflow_name,
                        old_step,
                        condition=transition.when,
                        result=True,
                        to_step=new_step,
                    ),
                )

                instance.current_step = new_step
                instance.step_action_count = 0
                instance.step_entered_at = datetime.now(UTC)
                instance_mgr.save_instance(instance)

                # Reset consecutive-tool-block counters so failures from the
                # previous step don't bleed into the new one
                variables["consecutive_tool_blocks"] = 0
                variables["_last_blocked_tool"] = ""
                variables["tool_block_pending"] = False

                logger.info(
                    f"Step transition: {old_step} -> {new_step} (workflow={instance.workflow_name}, session={session_id})",
                )

                transition_steps.append((old_step, new_step))
                current_step_def = new_step_def
                transition_taken = True

                # Evaluate exit_condition after transition
                if definition.exit_condition:
                    exit_ctx = {
                        "current_step": instance.current_step,
                        "vars": instance.variables,
                        "variables": variables,
                    }
                    exit_met = self._evaluate_condition(
                        definition.exit_condition, exit_ctx, "block"
                    )
                    self.workflow_audit.log_exit_check(
                        session_id=session_id,
                        step=instance.current_step,
                        condition=definition.exit_condition,
                        result="met" if exit_met else "unmet",
                        reason=("Exit condition met" if exit_met else "Exit condition was not met"),
                        context=self._step_audit_context(
                            instance.workflow_name,
                            instance.current_step,
                            condition=definition.exit_condition,
                            result=exit_met,
                        ),
                    )
                    if exit_met:
                        variables["step_workflow_complete"] = True
                        logger.info(
                            f"Exit condition met for workflow {instance.workflow_name} (session={session_id}, step={instance.current_step})",
                        )
                        await self._complete_agent_workflow_run(
                            session_id,
                            instance.workflow_name,
                        )

                break

            if not transition_taken:
                break
        else:
            logger.warning(
                "Stopped step transition chain for workflow %s (session=%s) after %d transitions",
                instance.workflow_name,
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
            instance_mgr.save_instance(instance)

        return None
