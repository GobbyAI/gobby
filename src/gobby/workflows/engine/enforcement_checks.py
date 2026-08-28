"""Agent and step tool allow/block checks for workflow enforcement."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookResponse
from gobby.storage.hub._ambient import ambient_transaction
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.enforcement.blocking import (
    canonical_gobby_tool_name,
    is_discovery_tool,
    is_gobby_call_tool,
    is_infrastructure_tool,
    is_operator_tool,
    is_provider_discovery_tool,
)
from gobby.workflows.engine.skill_load_guidance import skill_load_block_guidance
from gobby.workflows.reserved_variables import is_reserved_workflow_variable
from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager

if TYPE_CHECKING:
    from gobby.storage.workflow_audit import WorkflowAuditManager

logger = logging.getLogger("gobby.workflows.engine.enforcement")

_DENIAL_COUNTS_VARIABLE = "_enforcement_denial_counts"
_TERMINAL_DENIAL_COUNT = 3


def _step_tool_block_guidance(step_name: str) -> str:
    return (
        f"\nThis capability is unavailable for the rest of the '{step_name}' step. "
        "Abandon this call, continue with an allowed operation, and do not retry "
        "the same blocked tool in this step. Repeating an identical denial three "
        "times ends an autonomous run in a terminal blocked state."
    )


def _agent_tool_block_guidance() -> str:
    return (
        "\nAbandon this call and continue with an operation permitted by the agent "
        "definition. Do not retry the same denied target. Repeating an identical "
        "denial three times ends an autonomous run in a terminal blocked state."
    )


def _reserved_variable_block_guidance(variable_name: str, step_name: str | None) -> str:
    step_scope = f" in the '{step_name}' step" if step_name else ""
    return (
        f"\nAbandon only this write to variable '{variable_name}'{step_scope}; "
        "set_variable remains available for other permitted variables. Continue with "
        "an allowed operation and do not retry this variable write. Repeating an "
        "identical denial three times ends an autonomous run in a terminal blocked state."
    )


class EnforcementCheckMixin:
    """Tool restriction checks for agent and step workflow enforcement."""

    instance_manager: AgentStepInstanceManager
    _pending_terminal_denial: tuple[Any, str, str] | None = None

    if TYPE_CHECKING:
        workflow_audit: WorkflowAuditManager
        _runner: Any | None

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
            instance: AgentStepInstance,
            tool_name: str,
            mcp_server: str,
            mcp_tool_name: str,
            mcp_key: str,
            handler_tool_input: dict[str, Any],
        ) -> HookResponse | None: ...

    def _active_agent_run(self, session_id: str) -> tuple[Any, Any] | None:
        """Return the active run and its storage for this child session."""
        runner = getattr(self, "_runner", None)
        storage = getattr(runner, "run_storage", None)
        get_by_session = getattr(storage, "get_by_session", None)
        if not callable(get_by_session):
            return None
        run = get_by_session(session_id)
        if run is None:
            return None
        return run, storage

    def _bound_task_is_terminal(self, session_id: str) -> bool:
        """True when this child session's active run is bound to a closed task."""
        from gobby.storage.tasks import LocalTaskManager, TaskNotFoundError

        active = self._active_agent_run(session_id)
        if active is None:
            return False
        run, _storage = active
        task_id = getattr(run, "task_id", None)
        if not task_id:
            return False
        task_manager = getattr(self, "_task_manager", None)
        if task_manager is None:
            db = getattr(self, "db", None)
            if db is None:
                return False
            task_manager = LocalTaskManager(db)
        try:
            task = task_manager.get_task(task_id)
        except TaskNotFoundError:
            return False
        return task.closed_at is not None

    @staticmethod
    def _denial_scope(
        run_id: str,
        instance: AgentStepInstance,
        step: WorkflowStep,
    ) -> dict[str, str]:
        entered_at = instance.step_entered_at
        step_revision = (
            entered_at.isoformat()
            if entered_at is not None
            else f"{step.name}:{instance.total_action_count}"
        )
        return {
            "agent_run_id": run_id,
            "workflow_instance_id": instance.id,
            "step_revision": step_revision,
        }

    def _record_enforcement_denial(
        self,
        *,
        session_id: str,
        rule: str,
        target: str,
        reason: str,
        step: WorkflowStep | None = None,
        instance: AgentStepInstance | None = None,
    ) -> str:
        """Persist a run/instance/step/rule/target denial and terminalize on the third."""
        active_run = self._active_agent_run(session_id)
        if active_run is None:
            return reason
        run, storage = active_run

        if step is None or instance is None:
            resolved_step, resolved_instance = self._get_step_for_session(session_id)
            if resolved_step is None or resolved_instance is None:
                return reason
            step = resolved_step
            instance = resolved_instance

        scope = self._denial_scope(str(run.id), instance, step)
        raw_state = instance.variables.get(_DENIAL_COUNTS_VARIABLE)
        state = raw_state if isinstance(raw_state, dict) else {}
        raw_counts = state.get("counts") if state.get("scope") == scope else {}
        counts = dict(raw_counts) if isinstance(raw_counts, dict) else {}
        counter_key = json.dumps(
            {
                **scope,
                "rule": rule,
                "target": target,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        count = int(counts.get(counter_key, 0)) + 1
        counts[counter_key] = count
        instance.variables[_DENIAL_COUNTS_VARIABLE] = {
            "scope": scope,
            "counts": counts,
        }
        self.instance_manager.save(instance)

        if count < _TERMINAL_DENIAL_COUNT:
            return reason

        terminal_error = (
            "Agent run blocked after 3 identical enforcement denials: "
            f"workflow={instance.agent_name}, step={step.name}, "
            f"rule={rule}, target={target}"
        )
        self._pending_terminal_denial = (storage, str(run.id), terminal_error)
        if ambient_transaction(getattr(self, "db", None)) is None:
            self._flush_pending_terminal_denial()
        return (
            f"{reason}\nThe third identical denial transitioned agent run {run.id} "
            "to a terminal blocked state. The guarded step was not advanced."
        )

    def _flush_pending_terminal_denial(self) -> None:
        pending = self._pending_terminal_denial
        self._pending_terminal_denial = None
        if pending is None:
            return
        storage, run_id, terminal_error = pending
        storage.fail(run_id, terminal_error)

    def _reset_enforcement_denial_target(
        self,
        session_id: str,
        step: WorkflowStep,
        instance: AgentStepInstance,
        target: str,
    ) -> None:
        """Clear one target after that previously denied target becomes allowed."""
        active_run = self._active_agent_run(session_id)
        if active_run is None:
            return
        run, _storage = active_run
        state = instance.variables.get(_DENIAL_COUNTS_VARIABLE)
        if not isinstance(state, dict):
            return
        if state.get("scope") != self._denial_scope(str(run.id), instance, step):
            return
        raw_counts = state.get("counts")
        if not isinstance(raw_counts, dict):
            return

        counts = dict(raw_counts)
        for counter_key in tuple(counts):
            if not isinstance(counter_key, str):
                continue
            try:
                decoded_key = json.loads(counter_key)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded_key, dict) and decoded_key.get("target") == target:
                counts.pop(counter_key)

        if len(counts) == len(raw_counts):
            return
        if counts:
            state["counts"] = counts
        else:
            instance.variables.pop(_DENIAL_COUNTS_VARIABLE, None)
        self.instance_manager.save(instance)

    def _get_step_for_session(
        self, session_id: str
    ) -> tuple[WorkflowStep | None, AgentStepInstance | None]:
        """Get the current step and snapshot instance for a session.

        Returns (step, instance) or (None, None) if no active step workflow.
        """
        if not session_id:
            return None, None
        instance = self.instance_manager.get_for_session(session_id)
        if instance is None or not instance.enabled or not instance.current_step:
            return None, None
        step = instance.snapshot.get_step(instance.current_step)
        return step, instance

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
                reason = (
                    "Rule enforced by Gobby: [workflow-runtime-variable]\n"
                    f"Variable '{variable_name}' is managed by the workflow runtime."
                    f"{_reserved_variable_block_guidance(variable_name, None)}"
                )
                reason = self._record_enforcement_denial(
                    session_id=session_id,
                    rule="agent-runtime-variable",
                    target=f"variable:{variable_name.casefold()}",
                    reason=reason,
                )
                return HookResponse(
                    decision="block",
                    reason=reason,
                )

        blocked_tools: list[str] = variables.get("_agent_blocked_tools") or []
        blocked_mcp_tools: list[str] = variables.get("_agent_blocked_mcp_tools") or []
        if not blocked_tools and not blocked_mcp_tools:
            return None

        agent_type = variables.get("_agent_type", "unknown")
        canonical_tool = canonical_gobby_tool_name(tool_name)

        # Check native tool block-list first so explicit agent blocks override exemptions.
        if blocked_tools and canonical_tool in {
            canonical_gobby_tool_name(blocked) for blocked in blocked_tools
        }:
            reason = (
                f"Rule enforced by Gobby: [agent-enforcement:{agent_type}]\n"
                f"Tool '{tool_name}' is blocked for the '{agent_type}' agent."
                f"{_agent_tool_block_guidance()}"
            )
            reason = self._record_enforcement_denial(
                session_id=session_id,
                rule="agent-native-tool-block",
                target=f"tool:{canonical_tool.casefold()}",
                reason=reason,
            )
            return HookResponse(
                decision="block",
                reason=reason,
            )

        # Discovery/infrastructure tools pass unless explicitly blocked above.
        if is_provider_discovery_tool(tool_name):
            return None
        if canonical_tool.startswith("mcp__gobby__"):
            mcp_suffix = canonical_tool[len("mcp__gobby__") :]
            if is_discovery_tool(mcp_suffix) or is_infrastructure_tool(mcp_suffix):
                return None

        # Check MCP tool restrictions (for call_tool)
        if blocked_mcp_tools and is_gobby_call_tool(tool_name):
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
                    reason = (
                        f"Rule enforced by Gobby: [agent-enforcement:{agent_type}]\n"
                        f"MCP tool '{mcp_key}' is blocked for the '{agent_type}' agent."
                        f"{_agent_tool_block_guidance()}"
                    )
                    reason = self._record_enforcement_denial(
                        session_id=session_id,
                        rule="agent-mcp-tool-block",
                        target=f"mcp:{mcp_key.casefold()}",
                        reason=reason,
                    )
                    return HookResponse(
                        decision="block",
                        reason=reason,
                    )

        return None

    def _check_step_tool_enforcement(
        self, event: HookEvent, session_id: str, variables: dict[str, Any]
    ) -> HookResponse | None:
        """Check step-level tool restrictions. Returns block response or None to continue."""
        from gobby.storage.hub.protocol import AgentStepInstanceMutation

        db = getattr(self, "db", None)
        lock = AgentStepInstanceMutation(session_id=session_id)
        try:
            if db is not None:
                with db.transaction_immediate(lock):
                    return self._check_step_tool_enforcement_locked(event, session_id, variables)
            return self._check_step_tool_enforcement_locked(event, session_id, variables)
        finally:
            self._flush_pending_terminal_denial()

    def _check_step_tool_enforcement_locked(
        self, event: HookEvent, session_id: str, variables: dict[str, Any]
    ) -> HookResponse | None:
        step, instance = self._get_step_for_session(session_id)
        if step is None or instance is None:
            return None

        tool_name = event.data.get("tool_name", "")
        canonical_tool = canonical_gobby_tool_name(tool_name)
        wf_name = instance.agent_name
        allowed_target = f"tool:{canonical_tool.casefold()}"

        # Provider tool-catalog tools (Claude Code ToolSearch, Grok search_tool)
        # discover tools without executing any and are always allowed.
        if is_provider_discovery_tool(tool_name):
            return None

        if self._is_native_set_variable_tool(tool_name):
            tool_input = event.data.get("tool_input") or {}
            variable_name = ""
            if isinstance(tool_input, dict):
                allowed_target = self._normalized_enforcement_target(tool_name, tool_input)
                variable_name = self._is_reserved_variable_write(tool_name, tool_input) or ""
            if variable_name:
                reason = (
                    f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                    f"Variable '{variable_name}' is managed by the step workflow runtime."
                    f"{_reserved_variable_block_guidance(variable_name, step.name)}"
                )
                reason = self._record_enforcement_denial(
                    session_id=session_id,
                    rule="step-runtime-variable",
                    target=f"variable:{variable_name.casefold()}",
                    reason=reason,
                    step=step,
                    instance=instance,
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
        if canonical_tool.startswith("mcp__gobby__"):
            mcp_suffix = canonical_tool[len("mcp__gobby__") :]
            if is_discovery_tool(mcp_suffix) or is_infrastructure_tool(mcp_suffix):
                return None

        # Check native tool allow-list
        if step.allowed_tools != "all":
            if canonical_tool not in {
                canonical_gobby_tool_name(allowed) for allowed in step.allowed_tools
            }:
                guidance = skill_load_block_guidance(step)
                reason = (
                    f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                    f"Tool '{tool_name}' is not allowed in the '{step.name}' step.\n"
                    f"Allowed tools: {', '.join(step.allowed_tools)}{guidance}"
                    f"{_step_tool_block_guidance(step.name)}"
                )
                reason = self._record_enforcement_denial(
                    session_id=session_id,
                    rule="step-native-tool-allowlist",
                    target=f"tool:{canonical_tool.casefold()}",
                    reason=reason,
                    step=step,
                    instance=instance,
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
        if canonical_tool in {canonical_gobby_tool_name(blocked) for blocked in step.blocked_tools}:
            reason = (
                f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                f"Tool '{tool_name}' is blocked in the '{step.name}' step."
                f"{_step_tool_block_guidance(step.name)}"
            )
            reason = self._record_enforcement_denial(
                session_id=session_id,
                rule="step-native-tool-block",
                target=f"tool:{canonical_tool.casefold()}",
                reason=reason,
                step=step,
                instance=instance,
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
        if is_gobby_call_tool(tool_name):
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
                allowed_target = self._normalized_enforcement_target(
                    tool_name,
                    tool_input,
                    mcp_server=mcp_server,
                    mcp_tool_name=mcp_tool_name,
                )

                # A run whose bound task is already terminal has nothing left
                # for the step to enforce; blocking end_agent_run then strands
                # a finished worker in a spurious error state (#19554).
                if mcp_key == "gobby-agents:end_agent_run" and self._bound_task_is_terminal(
                    session_id
                ):
                    return None

                # Explicit blocks override default grants and allow-list exemptions.
                if mcp_key and step.blocked_mcp_tools:
                    if self._mcp_tool_matches(mcp_key, step.blocked_mcp_tools):
                        reason = (
                            f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                            f"MCP tool '{mcp_key}' is blocked in the '{step.name}' step."
                            f"{_step_tool_block_guidance(step.name)}"
                        )
                        reason = self._record_enforcement_denial(
                            session_id=session_id,
                            rule="step-mcp-tool-block",
                            target=f"mcp:{mcp_key.casefold()}",
                            reason=reason,
                            step=step,
                            instance=instance,
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

                # Structured handoff preserves the active agent and is capability-neutral.
                if mcp_tool_name == "set_handoff":
                    return None

                if mcp_key and step.allowed_mcp_tools != "all":
                    if not self._mcp_tool_matches(mcp_key, step.allowed_mcp_tools):
                        guidance = skill_load_block_guidance(step)
                        reason = (
                            f"Rule enforced by Gobby: [step-enforcement:{wf_name}/{step.name}]\n"
                            f"MCP tool '{mcp_key}' is not allowed in the '{step.name}' step.\n"
                            f"Allowed MCP tools: {', '.join(step.allowed_mcp_tools)}{guidance}"
                            f"{_step_tool_block_guidance(step.name)}"
                        )
                        reason = self._record_enforcement_denial(
                            session_id=session_id,
                            rule="step-mcp-tool-allowlist",
                            target=f"mcp:{mcp_key.casefold()}",
                            reason=reason,
                            step=step,
                            instance=instance,
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
                            f"{_reserved_variable_block_guidance(variable_name, step.name)}"
                        )
                        reason = self._record_enforcement_denial(
                            session_id=session_id,
                            rule="step-runtime-variable",
                            target=f"variable:{variable_name.casefold()}",
                            reason=reason,
                            step=step,
                            instance=instance,
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

        self._reset_enforcement_denial_target(
            session_id,
            step,
            instance,
            allowed_target,
        )
        return None

    @staticmethod
    def _normalized_enforcement_target(
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        mcp_server: str = "",
        mcp_tool_name: str = "",
    ) -> str:
        """Return the normalized target shared by deny counting and successful unblock."""
        if EnforcementCheckMixin._is_native_set_variable_tool(tool_name):
            variable_name = str(tool_input.get("name") or tool_input.get("variable") or "")
            if variable_name:
                return f"variable:{variable_name.casefold()}"
        if mcp_tool_name == "set_variable":
            resolved_input = EnforcementCheckMixin._step_handler_tool_input(tool_input)
            variable_name = str(resolved_input.get("name") or resolved_input.get("variable") or "")
            if variable_name:
                return f"variable:{variable_name.casefold()}"
        if mcp_server and mcp_tool_name:
            return f"mcp:{mcp_server.casefold()}:{mcp_tool_name.casefold()}"
        return f"tool:{canonical_gobby_tool_name(tool_name).casefold()}"

    @staticmethod
    def _is_reserved_variable_write(
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        mcp_tool_name: str | None = None,
    ) -> str | None:
        """Return the reserved variable name for blocked user writes."""
        is_native_set_variable = EnforcementCheckMixin._is_native_set_variable_tool(tool_name)
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
        return canonical_gobby_tool_name(tool_name) == "mcp__gobby__set_variable"
