"""
Workflow dry-run evaluator.

Validates workflow definitions structurally and semantically without executing them.
Used standalone via `gobby workflows check` or embedded in spawn evaluation.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from gobby.workflows.definitions import (
    AgentDefinitionBody,
    WorkflowDefinition,
    WorkflowStep,
)
from gobby.workflows.dry_run_validation import analyze_condition
from gobby.workflows.handler_route_lint import check_handler_routes
from gobby.workflows.native_tools import is_known_native_tool

if TYPE_CHECKING:
    from gobby.workflows.pipeline_loader import PipelineLoader

logger = logging.getLogger(__name__)


class MCPInventoryProtocol(Protocol):
    def get_available_servers(self) -> list[str]:
        """Return available MCP server names."""
        ...

    async def list_tools(self) -> dict[str, list[dict[str, Any]]]:
        """Return MCP tools grouped by server name."""
        ...


@dataclass
class EvaluationItem:
    """A single finding from workflow or spawn evaluation."""

    layer: str  # "structure", "semantics", "workflow_resolution", etc.
    level: str  # "error", "warning", "info"
    code: str  # e.g., "UNREACHABLE_STEP", "DEAD_END_STEP"
    message: str
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "layer": self.layer,
            "level": self.level,
            "code": self.code,
            "message": self.message,
        }
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class WorkflowStepTrace:
    """Summary of a single workflow step for dry-run output."""

    name: str
    description: str | None
    on_enter_actions: list[str]
    allowed_tools: list[str] | str
    blocked_tools: list[str]
    allowed_mcp_tools: list[str] | str
    blocked_mcp_tools: list[str]
    transitions: list[dict[str, str]]
    on_mcp_success: list[str]
    on_mcp_error: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "on_enter_actions": self.on_enter_actions,
            "allowed_tools": self.allowed_tools,
            "blocked_tools": self.blocked_tools,
            "allowed_mcp_tools": self.allowed_mcp_tools,
            "blocked_mcp_tools": self.blocked_mcp_tools,
            "transitions": self.transitions,
            "on_mcp_success": self.on_mcp_success,
            "on_mcp_error": self.on_mcp_error,
        }


@dataclass
class WorkflowEvaluation:
    """Result of evaluating a workflow definition."""

    valid: bool
    items: list[EvaluationItem] = field(default_factory=list)
    workflow_name: str | None = None
    step_trace: list[WorkflowStepTrace] = field(default_factory=list)
    lifecycle_path: list[str] = field(default_factory=list)
    variables_declared: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "items": [i.to_dict() for i in self.items],
            "workflow_name": self.workflow_name,
            "step_trace": [s.to_dict() for s in self.step_trace],
            "lifecycle_path": self.lifecycle_path,
            "variables_declared": self.variables_declared,
        }

    @property
    def errors(self) -> list[EvaluationItem]:
        return [i for i in self.items if i.level == "error"]

    @property
    def warnings(self) -> list[EvaluationItem]:
        return [i for i in self.items if i.level == "warning"]


# ---- Jinja variable reference pattern ----
_JINJA_VAR_RE = re.compile(r"\{\{\s*variables\.(\w+)\s*\}\}")

# Built-in variables that don't need to be declared
_BUILTIN_VARIABLES = {
    "session_id",
    "project_path",
    "project_id",
    "session_task",
    "mcp_calls",
    "mcp_results",
}


async def evaluate_pipeline_definition(
    name: str,
    workflow_loader: PipelineLoader,
    project_id: str | None = None,
    mcp_manager: MCPInventoryProtocol | None = None,
) -> WorkflowEvaluation:
    """Evaluate a pipeline definition. Agent steps use evaluate_agent_definition."""
    result = WorkflowEvaluation(valid=True, workflow_name=name)

    try:
        definition = await workflow_loader.load_pipeline(name, project_id)
    except ValueError as e:
        result.valid = False
        result.items.append(
            EvaluationItem(
                layer="structure",
                level="error",
                code="WORKFLOW_LOAD_ERROR",
                message=f"Failed to load pipeline '{name}': {e}",
            )
        )
        return result

    if definition is None:
        result.valid = False
        result.items.append(
            EvaluationItem(
                layer="structure",
                level="error",
                code="WORKFLOW_NOT_FOUND",
                message=f"Workflow '{name}' not found",
            )
        )
        return result

    result.items.append(
        EvaluationItem(
            layer="structure",
            level="info",
            code="PIPELINE_TYPE",
            message=f"'{name}' is a pipeline workflow — step checks skipped",
        )
    )
    return result


async def evaluate_agent_definition(
    agent: AgentDefinitionBody,
    mcp_manager: MCPInventoryProtocol | None = None,
) -> WorkflowEvaluation:
    """
    Evaluate an agent definition's tool gates and inline step workflow.

    Lints the agent-level blocked_tools/blocked_mcp_tools (enforced at runtime
    for every step) and, when the agent carries inline steps, runs the full
    structural and semantic step checks over them.

    Args:
        agent: Parsed agent definition body.
        mcp_manager: Optional MCP inventory for semantic MCP tool checks.

    Returns:
        WorkflowEvaluation with findings.
    """
    result = WorkflowEvaluation(valid=True, workflow_name=agent.name)

    check_agent_tool_gates(agent, result)

    if agent.step_workflow is None:
        result.items.append(
            EvaluationItem(
                layer="structure",
                level="info",
                code="NO_STEP_WORKFLOW",
                message=f"Agent '{agent.name}' has no step workflow",
            )
        )
    else:
        result.variables_declared = list((agent.step_workflow.variables or {}).keys())
        inline = WorkflowDefinition(
            name=f"{agent.name} (inline steps)",
            type="step",
            steps=agent.step_workflow.steps,
            variables=agent.step_workflow.variables or {},
            exit_condition=agent.step_workflow.exit_condition,
        )
        # check_agent_tool_gates already covered inline step gates; the
        # structural pass re-adds them, so dedupe by (code, message).
        seen = {(i.code, i.message) for i in result.items}
        inline_result = WorkflowEvaluation(valid=True, workflow_name=inline.name)
        _check_structure(inline, inline_result)
        await _check_semantics(inline, inline_result, mcp_manager)
        result.items.extend(i for i in inline_result.items if (i.code, i.message) not in seen)
        from gobby.workflows.dry_run_trace import _build_lifecycle_path, _build_step_trace

        _build_step_trace(inline, result)
        _build_lifecycle_path(inline, result)

    if mcp_manager is not None:
        # Agent-level blocked_mcp_tools against the live inventory (fail-open).
        try:
            available_servers = set(mcp_manager.get_available_servers())
            tools_by_server = await mcp_manager.list_tools()
            server_tools = {
                server_name: {t.get("name", "") for t in tools if isinstance(t, dict)}
                for server_name, tools in tools_by_server.items()
            }
            _check_mcp_tool_refs(
                f"Agent '{agent.name}'",
                "blocked_mcp_tools",
                agent.blocked_mcp_tools,
                available_servers,
                server_tools,
                result,
                level="error",
            )
        except (ConnectionError, TimeoutError, RuntimeError, OSError) as e:
            result.items.append(
                EvaluationItem(
                    layer="semantics",
                    level="warning",
                    code="MCP_QUERY_FAILED",
                    message=f"Failed to query MCP servers: {e}",
                )
            )

    if any(i.level == "error" for i in result.items):
        result.valid = False

    return result


def _check_structure(definition: WorkflowDefinition, result: WorkflowEvaluation) -> None:
    """Run structural checks on a workflow definition."""
    steps = definition.steps

    # No steps defined
    if len(steps) == 0:
        result.items.append(
            EvaluationItem(
                layer="structure",
                level="error",
                code="NO_STEPS",
                message="Workflow has no steps defined",
            )
        )
        return

    step_names = [s.name for s in steps]

    # Duplicate step names
    seen: set[str] = set()
    for name in step_names:
        if name in seen:
            result.items.append(
                EvaluationItem(
                    layer="structure",
                    level="error",
                    code="DUPLICATE_STEP_NAME",
                    message=f"Duplicate step name: '{name}'",
                    detail={"step": name},
                )
            )
        seen.add(name)

    step_name_set = set(step_names)

    if definition.exit_condition:
        _check_condition(
            definition.exit_condition,
            "exit_condition",
            {"current_step", "vars", "variables"},
            result,
        )

    # Undefined transition targets
    for step in steps:
        _warn_unexecuted_actions(step.name, "on_enter", step.on_enter, result)
        _warn_unexecuted_actions(step.name, "on_exit", step.on_exit, result)
        for transition in step.transitions:
            _check_condition(
                transition.when,
                "transition",
                {"vars"},
                result,
                step=step.name,
            )
            _warn_unexecuted_actions(step.name, "on_transition", transition.on_transition, result)
            if transition.to not in step_name_set:
                result.items.append(
                    EvaluationItem(
                        layer="structure",
                        level="error",
                        code="UNDEFINED_TRANSITION_TARGET",
                        message=f"Step '{step.name}' transitions to undefined step '{transition.to}'",
                        detail={"from": step.name, "to": transition.to},
                    )
                )

        for handler in step.on_mcp_before:
            if isinstance(handler, dict) and isinstance(handler.get("when"), str):
                _check_condition(
                    handler["when"],
                    "on_mcp_before",
                    {"vars", "variables", "tool_input"},
                    result,
                    step=step.name,
                )
        for handler in [*step.on_mcp_success, *step.on_mcp_error]:
            if isinstance(handler, dict) and isinstance(handler.get("when"), str):
                _check_condition(
                    handler["when"],
                    "on_mcp_handler",
                    {"vars", "tool_input", "tool_output"},
                    result,
                    step=step.name,
                )

    # Unreachable steps (BFS from first step)
    if steps:
        reachable = _bfs_reachable(steps[0].name, steps, step_name_set)
        for step in steps:
            if step.name not in reachable:
                result.items.append(
                    EvaluationItem(
                        layer="structure",
                        level="warning",
                        code="UNREACHABLE_STEP",
                        message=f"Step '{step.name}' is not reachable from the initial step",
                        detail={"step": step.name},
                    )
                )

    # Dead-end steps (steps that cannot transition or satisfy a step-based exit)
    exit_condition_names: set[str] = set()
    if definition.exit_condition:
        # Parse exit_condition for step name references (simple heuristic)
        for sn in step_names:
            if sn in definition.exit_condition:
                exit_condition_names.add(sn)

    for step in steps:
        if not step.transitions and step.name not in exit_condition_names:
            result.items.append(
                EvaluationItem(
                    layer="structure",
                    level="warning",
                    code="DEAD_END_STEP",
                    message=(
                        f"Step '{step.name}' has no transitions and is not selected by the "
                        "workflow exit condition"
                    ),
                    detail={"step": step.name},
                )
            )

    # Circular-only path detection
    if steps and steps[0].transitions:
        has_terminal = _has_terminal_path(steps[0].name, steps, step_name_set)
        if not has_terminal:
            result.items.append(
                EvaluationItem(
                    layer="structure",
                    level="warning",
                    code="CIRCULAR_ONLY_PATH",
                    message="All paths from the initial step loop without reaching a terminal step",
                )
            )

    # Undefined variable references in on_enter actions
    declared_vars = set(definition.variables.keys()) | _BUILTIN_VARIABLES
    for step in steps:
        for action in step.on_enter:
            action_str = str(action)
            for match in _JINJA_VAR_RE.finditer(action_str):
                var_name = match.group(1)
                if var_name not in declared_vars:
                    result.items.append(
                        EvaluationItem(
                            layer="structure",
                            level="warning",
                            code="UNDEFINED_VARIABLE_REF",
                            message=f"Step '{step.name}' references undeclared variable '{var_name}'",
                            detail={"step": step.name, "variable": var_name},
                        )
                    )

    # Tool gate reference validation (typo in a blocked list fails open)
    for step in steps:
        check_step_tool_gates(step, result)

    # Tool restriction conflicts
    for step in steps:
        if isinstance(step.allowed_tools, list) and step.blocked_tools:
            overlap = set(step.allowed_tools) & set(step.blocked_tools)
            if overlap:
                result.items.append(
                    EvaluationItem(
                        layer="structure",
                        level="warning",
                        code="TOOL_RESTRICTION_CONFLICT",
                        message=f"Step '{step.name}' has tools in both allowed and blocked: {sorted(overlap)}",
                        detail={"step": step.name, "tools": sorted(overlap)},
                    )
                )

        if isinstance(step.allowed_mcp_tools, list) and step.blocked_mcp_tools:
            overlap = set(step.allowed_mcp_tools) & set(step.blocked_mcp_tools)
            if overlap:
                result.items.append(
                    EvaluationItem(
                        layer="structure",
                        level="warning",
                        code="MCP_TOOL_RESTRICTION_CONFLICT",
                        message=f"Step '{step.name}' has MCP tools in both allowed and blocked: {sorted(overlap)}",
                        detail={"step": step.name, "mcp_tools": sorted(overlap)},
                    )
                )

    for finding in check_handler_routes(definition):
        result.items.append(
            EvaluationItem(
                layer="semantics",
                level="warning",
                code=finding.code,
                message=finding.message,
                detail=finding.detail,
            )
        )


def _check_condition(
    condition: str,
    condition_type: str,
    runtime_names: set[str],
    result: WorkflowEvaluation,
    *,
    step: str | None = None,
) -> None:
    """Validate condition syntax and names against its runtime context."""
    detail: dict[str, Any] = {"condition": condition, "condition_type": condition_type}
    if step is not None:
        detail["step"] = step

    syntax_error, unknown_names = analyze_condition(condition, runtime_names)
    if syntax_error:
        detail["error"] = syntax_error
        result.items.append(
            EvaluationItem(
                layer="structure",
                level="error",
                code="INVALID_CONDITION_SYNTAX",
                message=f"Invalid {condition_type} condition syntax: {syntax_error}",
                detail=detail,
            )
        )
        return

    if not unknown_names:
        return

    detail["names"] = unknown_names
    result.items.append(
        EvaluationItem(
            layer="structure",
            level="warning",
            code="CONDITION_UNKNOWN_NAME",
            message=(
                f"{condition_type} condition references names absent from its runtime context: "
                f"{unknown_names}"
            ),
            detail=detail,
        )
    )


def _warn_unexecuted_actions(
    step_name: str,
    field_name: str,
    actions: list[dict[str, Any]],
    result: WorkflowEvaluation,
) -> None:
    """Warn about lifecycle action lists that the runtime does not execute."""
    if not actions:
        return
    result.items.append(
        EvaluationItem(
            layer="structure",
            level="warning",
            code="ACTION_NOT_EXECUTED",
            message=f"Step '{step_name}' {field_name} actions are not executed by the runtime",
            detail={"step": step_name, "field": field_name, "action_count": len(actions)},
        )
    )


def check_step_tool_gates(step: WorkflowStep, result: WorkflowEvaluation) -> None:
    """Statically validate a step's tool gate references.

    Runtime gate matching is exact string membership, so an unrecognized name
    in a blocked list is a security control that silently never fires
    (fail-open, error) while one in an allowed list merely over-restricts
    (fail-closed, warning).
    """
    owner = f"Step '{step.name}'"
    _check_native_tool_refs(owner, "blocked_tools", step.blocked_tools, result, blocking=True)
    _check_native_tool_refs(owner, "allowed_tools", step.allowed_tools, result, blocking=False)
    _check_mcp_ref_format(owner, "blocked_mcp_tools", step.blocked_mcp_tools, result, blocking=True)
    _check_mcp_ref_format(
        owner, "allowed_mcp_tools", step.allowed_mcp_tools, result, blocking=False
    )


def check_agent_tool_gates(agent: AgentDefinitionBody, result: WorkflowEvaluation) -> None:
    """Statically validate an agent definition's tool gates.

    Covers the agent-level ``blocked_tools``/``blocked_mcp_tools`` (enforced
    for every step at runtime) plus each inline step's gates.
    """
    owner = f"Agent '{agent.name}'"
    _check_native_tool_refs(owner, "blocked_tools", agent.blocked_tools, result, blocking=True)
    _check_mcp_ref_format(
        owner, "blocked_mcp_tools", agent.blocked_mcp_tools, result, blocking=True
    )
    for step in agent.step_workflow.steps if agent.step_workflow else []:
        check_step_tool_gates(step, result)


def _check_native_tool_refs(
    owner: str,
    field_name: str,
    tools: list[str] | str,
    result: WorkflowEvaluation,
    *,
    blocking: bool,
) -> None:
    """Flag native tool gate entries the catalog does not recognize."""
    if not isinstance(tools, list):
        return
    for ref in tools:
        if ":" in ref:
            mcp_field_name = field_name.replace("_tools", "_mcp_tools")
            consequence = (
                "the native-tool block never fires (fail-open)"
                if blocking
                else "the MCP tool is never allowed by this native-tool field"
            )
            _flag_gate_ref(
                owner,
                field_name,
                ref,
                result,
                blocking=blocking,
                code="MCP_TOOL_REF_IN_NATIVE_GATE",
                message=(
                    f"{owner} {field_name} contains MCP ref '{ref}'; use "
                    f"{mcp_field_name} instead — {consequence}"
                ),
            )
            continue
        if is_known_native_tool(ref):
            continue
        consequence = (
            "the block never fires (fail-open)" if blocking else "the tool is never allowed"
        )
        _flag_gate_ref(
            owner,
            field_name,
            ref,
            result,
            blocking=blocking,
            code="UNKNOWN_NATIVE_TOOL",
            message=(
                f"{owner} {field_name} references unknown native tool '{ref}' — {consequence}"
            ),
        )


def _check_mcp_ref_format(
    owner: str,
    field_name: str,
    tools: list[str] | str,
    result: WorkflowEvaluation,
    *,
    blocking: bool,
) -> None:
    """Flag MCP gate entries that can never match runtime 'server:tool' keys."""
    if not isinstance(tools, list):
        return
    for ref in tools:
        if ":" in ref:
            continue
        consequence = (
            "the block never fires (fail-open)" if blocking else "the tool is never allowed"
        )
        _flag_gate_ref(
            owner,
            field_name,
            ref,
            result,
            blocking=blocking,
            code="MALFORMED_MCP_TOOL_REF",
            message=(
                f"{owner} {field_name} entry '{ref}' is not 'server:tool' or "
                f"'server:*' — {consequence}"
            ),
        )


def _flag_gate_ref(
    owner: str,
    field_name: str,
    ref: str,
    result: WorkflowEvaluation,
    *,
    blocking: bool,
    code: str,
    message: str,
) -> None:
    result.items.append(
        EvaluationItem(
            layer="structure",
            level="error" if blocking else "warning",
            code=code,
            message=message,
            detail={"owner": owner, "field": field_name, "ref": ref},
        )
    )


def _bfs_reachable(
    start: str,
    steps: list[Any],
    valid_names: set[str],
) -> set[str]:
    """BFS from start step, returning all reachable step names."""
    step_map = {s.name: s for s in steps}
    visited: set[str] = set()
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        if current in visited or current not in valid_names:
            continue
        visited.add(current)
        step = step_map.get(current)
        if step:
            for t in step.transitions:
                if t.to not in visited:
                    queue.append(t.to)
    return visited


def _has_terminal_path(
    start: str,
    steps: list[Any],
    valid_names: set[str],
) -> bool:
    """Check if there's at least one path from start to a terminal step (no transitions)."""
    step_map = {s.name: s for s in steps}
    visited: set[str] = set()
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        if current in visited or current not in valid_names:
            continue
        visited.add(current)
        step = step_map.get(current)
        if step and not step.transitions:
            return True  # Found a terminal step
        if step:
            for t in step.transitions:
                if t.to not in visited:
                    queue.append(t.to)
    return False


async def _check_semantics(
    definition: WorkflowDefinition,
    result: WorkflowEvaluation,
    mcp_manager: MCPInventoryProtocol | None,
) -> None:
    """Run semantic checks that require live MCP connection."""
    if mcp_manager is None:
        result.items.append(
            EvaluationItem(
                layer="semantics",
                level="info",
                code="SEMANTIC_CHECKS_SKIPPED",
                message="Semantic checks skipped (no MCP connection)",
            )
        )
        return

    # Get available servers and their tools
    available_servers: set[str] = set()
    server_tools: dict[str, set[str]] = {}
    try:
        servers = mcp_manager.get_available_servers()
        available_servers = set(servers)
        tools_by_server = await mcp_manager.list_tools()
        for server_name, tools in tools_by_server.items():
            server_tools[server_name] = {t.get("name", "") for t in tools if isinstance(t, dict)}
    except (ConnectionError, TimeoutError, RuntimeError, OSError) as e:
        result.items.append(
            EvaluationItem(
                layer="semantics",
                level="warning",
                code="MCP_QUERY_FAILED",
                message=f"Failed to query MCP servers: {e}",
            )
        )
        return

    for step in definition.steps:
        # Check allowed_mcp_tools (unknown ref over-restricts: fail-closed)
        _check_mcp_tool_refs(
            f"Step '{step.name}'",
            "allowed_mcp_tools",
            step.allowed_mcp_tools,
            available_servers,
            server_tools,
            result,
            level="warning",
        )
        # Check blocked_mcp_tools (unknown ref never blocks: fail-open)
        _check_mcp_tool_refs(
            f"Step '{step.name}'",
            "blocked_mcp_tools",
            step.blocked_mcp_tools,
            available_servers,
            server_tools,
            result,
            level="error",
        )

        for handler in [*step.on_mcp_before, *step.on_mcp_success, *step.on_mcp_error]:
            _check_mcp_handler_ref(step.name, handler, available_servers, server_tools, result)


def _check_mcp_tool_refs(
    owner: str,
    field_name: str,
    tools: list[str] | str,
    available_servers: set[str],
    server_tools: dict[str, set[str]],
    result: WorkflowEvaluation,
    level: str = "warning",
) -> None:
    """Check MCP tool references in allowed/blocked lists.

    Pass level="error" for blocked lists: an unknown ref there means the
    block never matches at runtime (fail-open).
    """
    if tools == "all" or not isinstance(tools, list):
        return

    for ref in tools:
        if ":" not in ref:
            continue
        parts = ref.split(":", 1)
        server = parts[0]
        tool = parts[1] if len(parts) > 1 else ""

        if server not in available_servers:
            result.items.append(
                EvaluationItem(
                    layer="semantics",
                    level=level,
                    code="UNKNOWN_MCP_SERVER",
                    message=f"{owner} {field_name} references unknown server '{server}'",
                    detail={"owner": owner, "server": server, "ref": ref},
                )
            )
        elif server in server_tools and tool and tool != "*" and tool not in server_tools[server]:
            result.items.append(
                EvaluationItem(
                    layer="semantics",
                    level=level,
                    code="UNKNOWN_MCP_TOOL",
                    message=f"{owner} {field_name} references unknown tool '{ref}'",
                    detail={"owner": owner, "server": server, "tool": tool, "ref": ref},
                )
            )


def _check_mcp_handler_ref(
    step_name: str,
    handler: object,
    available_servers: set[str],
    server_tools: dict[str, set[str]],
    result: WorkflowEvaluation,
) -> None:
    """Check MCP tool references in success/error handlers."""
    if not isinstance(handler, dict):
        return
    server = handler.get("server")
    tool = handler.get("tool")
    if not isinstance(server, str) or not server:
        return
    if not isinstance(tool, str) or not tool:
        return
    ref = f"{server}:{tool}"
    if server not in available_servers:
        result.items.append(
            EvaluationItem(
                layer="semantics",
                level="warning",
                code="UNKNOWN_MCP_HANDLER_TARGET",
                message=f"Step '{step_name}' handler references unknown server '{server}'",
                detail={"step": step_name, "server": server, "ref": ref},
            )
        )
    elif server in server_tools and tool not in server_tools[server]:
        result.items.append(
            EvaluationItem(
                layer="semantics",
                level="warning",
                code="UNKNOWN_MCP_HANDLER_TARGET",
                message=f"Step '{step_name}' handler references unknown tool '{ref}'",
                detail={"step": step_name, "server": server, "tool": tool, "ref": ref},
            )
        )
