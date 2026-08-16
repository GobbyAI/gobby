"""Step workflow handler evaluation for MCP tool enforcement."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookResponse
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.step_instances import AgentStepInstanceManager

if TYPE_CHECKING:
    from gobby.storage.workflow_audit import WorkflowAuditManager

logger = logging.getLogger("gobby.workflows.engine.enforcement")


class EnforcementHandlerMixin:
    """Evaluate workflow on_mcp_before/on_mcp_success handler values."""

    instance_manager: AgentStepInstanceManager

    if TYPE_CHECKING:
        workflow_audit: WorkflowAuditManager

        def _evaluate_condition(
            self, condition: str, ctx: dict[str, Any], effect_type: str
        ) -> bool: ...

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
                        instance.agent_name,
                        step.name,
                        mcp_key,
                        str(var_name),
                        var_value,
                    )
                continue

            if action == "block":
                if vars_changed:
                    instance_mgr.save(instance)
                raw_reason = str(
                    handler.get("reason")
                    or (f"MCP tool '{mcp_key}' is blocked by a workflow on_mcp_before handler.")
                )
                reason = (
                    f"Rule enforced by Gobby: "
                    f"[step-enforcement:{instance.agent_name}/{step.name}]\n"
                    f"{raw_reason}"
                )
                self._audit_step_tool_call(
                    session_id,
                    instance.agent_name,
                    step.name,
                    tool_name,
                    "block",
                    reason=reason,
                    mcp_key=mcp_key,
                )
                return HookResponse(decision="block", reason=reason)

        if vars_changed:
            instance_mgr.save(instance)
        return None
