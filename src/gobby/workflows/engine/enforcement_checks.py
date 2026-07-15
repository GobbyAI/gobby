"""Agent and step tool allow/block checks for workflow enforcement."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pydantic

from gobby.hooks.events import HookEvent, HookResponse
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowDefinition, WorkflowStep
from gobby.workflows.enforcement.blocking import (
    is_discovery_tool,
    is_infrastructure_tool,
    is_operator_tool,
)
from gobby.workflows.engine.skill_load_guidance import skill_load_block_guidance
from gobby.workflows.reserved_variables import is_reserved_workflow_variable
from gobby.workflows.state_manager import WorkflowInstanceManager

if TYPE_CHECKING:
    from gobby.storage.workflow_audit import WorkflowAuditManager

logger = logging.getLogger("gobby.workflows.engine.enforcement")


class EnforcementCheckMixin:
    """Tool restriction checks for agent and step workflow enforcement."""

    instance_manager: WorkflowInstanceManager
    definition_manager: LocalWorkflowDefinitionManager

    if TYPE_CHECKING:
        workflow_audit: WorkflowAuditManager

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
        ) -> HookResponse | None: ...

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
        tool_name = event.data.get("tool_name", "")
        tool_input = event.data.get("tool_input") or {}
        if isinstance(tool_input, dict):
            mcp_tool_name = tool_input.get("tool_name")
            variable_name = self._is_reserved_variable_write(
                tool_name,
                tool_input,
                mcp_tool_name=mcp_tool_name if isinstance(mcp_tool_name, str) else None,
            )
            if variable_name:
                return HookResponse(
                    decision="block",
                    reason=(
                        "Rule enforced by Gobby: [workflow-runtime-variable]\n"
                        f"Variable '{variable_name}' is managed by the workflow runtime."
                    ),
                )

        blocked_tools: list[str] = variables.get("_agent_blocked_tools") or []
        blocked_mcp_tools: list[str] = variables.get("_agent_blocked_mcp_tools") or []
        if not blocked_tools and not blocked_mcp_tools:
            return None

        agent_type = variables.get("_agent_type", "unknown")

        # Check native tool block-list first so explicit agent blocks override exemptions.
        if blocked_tools and tool_name in blocked_tools:
            return HookResponse(
                decision="block",
                reason=(
                    f"Rule enforced by Gobby: [agent-enforcement:{agent_type}]\n"
                    f"Tool '{tool_name}' is blocked for the '{agent_type}' agent."
                ),
            )

        # Discovery/infrastructure tools pass unless explicitly blocked above.
        if tool_name.startswith("mcp__gobby__"):
            mcp_suffix = tool_name[len("mcp__gobby__") :]
            if is_discovery_tool(mcp_suffix) or is_infrastructure_tool(mcp_suffix):
                return None

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
            EnforcementCheckMixin._step_handler_tool_input(tool_input)
            if is_mcp_set_variable
            else tool_input
        )
        variable_name = str(resolved_input.get("name") or resolved_input.get("variable") or "")
        if is_reserved_workflow_variable(variable_name):
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
