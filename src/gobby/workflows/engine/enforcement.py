"""Agent and step tool enforcement for the rule engine.

Handles tool allow/block lists at the agent and step workflow levels,
MCP tool matching, and step workflow transition processing.
"""

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pydantic

from gobby.hooks.events import HookEvent, HookResponse
from gobby.storage.database import DatabaseProtocol
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowDefinition, WorkflowStep
from gobby.workflows.enforcement.blocking import (
    is_discovery_tool,
    is_infrastructure_tool,
)
from gobby.workflows.state_manager import WorkflowInstanceManager

logger = logging.getLogger(__name__)


class EnforcementMixin:
    """Mixin providing tool enforcement methods for RuleEngine."""

    db: DatabaseProtocol
    instance_manager: WorkflowInstanceManager
    definition_manager: LocalWorkflowDefinitionManager

    if TYPE_CHECKING:
        # Provided by TemplatingMixin at runtime via RuleEngine MRO
        def _evaluate_condition(
            self, condition: str, ctx: dict[str, Any], effect_type: str
        ) -> bool: ...

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
        self, event: HookEvent, session_id: str
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

        # Discovery/infrastructure tools always pass
        if tool_name.startswith("mcp__gobby__"):
            mcp_suffix = tool_name[len("mcp__gobby__") :]
            if is_discovery_tool(mcp_suffix) or is_infrastructure_tool(mcp_suffix):
                return None

        # Check native tool allow-list
        if step.allowed_tools != "all":
            if tool_name not in step.allowed_tools:
                return HookResponse(
                    decision="block",
                    reason=(
                        f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                        f"Tool '{tool_name}' is not allowed in the '{step.name}' step.\n"
                        f"Allowed tools: {', '.join(step.allowed_tools)}"
                    ),
                )

        # Check native tool block-list
        if tool_name in step.blocked_tools:
            return HookResponse(
                decision="block",
                reason=(
                    f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                    f"Tool '{tool_name}' is blocked in the '{step.name}' step."
                ),
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

                mcp_key = f"{mcp_server}:{mcp_tool_name}" if mcp_server and mcp_tool_name else ""

                if mcp_key and step.allowed_mcp_tools != "all":
                    if not self._mcp_tool_matches(mcp_key, step.allowed_mcp_tools):
                        return HookResponse(
                            decision="block",
                            reason=(
                                f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                                f"MCP tool '{mcp_key}' is not allowed in the '{step.name}' step.\n"
                                f"Allowed MCP tools: {', '.join(step.allowed_mcp_tools)}"
                            ),
                        )

                if mcp_key and step.blocked_mcp_tools:
                    if self._mcp_tool_matches(mcp_key, step.blocked_mcp_tools):
                        return HookResponse(
                            decision="block",
                            reason=(
                                f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                                f"MCP tool '{mcp_key}' is blocked in the '{step.name}' step."
                            ),
                        )

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

    def _process_step_after_tool(
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
        if tool_name not in ("call_tool", "mcp__gobby__call_tool"):
            return None

        tool_input = event.data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        mcp_server = tool_input.get("server_name", "")
        mcp_tool_name = tool_input.get("tool_name", "")
        if not mcp_server or not mcp_tool_name:
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

        # Execute handlers (on_mcp_success or on_mcp_error based on tool output)
        for handler in handlers:
            if handler.get("server") == mcp_server and handler.get("tool") == mcp_tool_name:
                handler_when = handler.get("when")
                if handler_when and not self._evaluate_condition(
                    handler_when,
                    {
                        "vars": {**instance.variables, **variables},
                        "tool_input": handler_tool_input,
                        "tool_output": tool_output,
                    },
                    str(handler.get("action") or "set_variable"),
                ):
                    continue
                if handler.get("action") == "set_variable":
                    var_name = handler.get("variable")
                    var_value = handler.get("value")
                    if var_name is not None:
                        instance.variables[var_name] = var_value
                        variables[var_name] = var_value
                        vars_changed = True

        # Skip transitions when tool failed and no error handlers modified state
        if is_app_failure and not vars_changed:
            return None

        # Evaluate transitions
        for transition in step.transitions:
            ctx = {"vars": {**instance.variables, **variables}}
            if not transition.when or self._evaluate_condition(transition.when, ctx, "transition"):
                old_step = instance.current_step
                new_step = transition.to

                if not definition.get_step(new_step):
                    logger.warning(
                        f"Transition to unknown step '{new_step}' in workflow '{instance.workflow_name}'",
                    )
                    continue

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

                # Evaluate exit_condition after transition
                if definition.exit_condition:
                    exit_ctx = {
                        "current_step": instance.current_step,
                        "vars": instance.variables,
                        "variables": variables,
                    }
                    if self._evaluate_condition(definition.exit_condition, exit_ctx, "block"):
                        variables["step_workflow_complete"] = True
                        logger.info(
                            f"Exit condition met for workflow {instance.workflow_name} (session={session_id}, step={instance.current_step})",
                        )

                # Build transition notification for AfterTool additionalContext
                new_step_def = definition.get_step(new_step)
                status_msg = (new_step_def.status_message or "").strip() if new_step_def else ""
                transition_notice = f"Step transition: {old_step} → {new_step}"
                if status_msg:
                    transition_notice += f"\n{status_msg}"
                return transition_notice  # First matching transition wins

        # Save if variables changed without transition
        if vars_changed:
            instance_mgr.save_instance(instance)

        return None
