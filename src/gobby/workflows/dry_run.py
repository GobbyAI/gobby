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
from typing import TYPE_CHECKING, Any

from gobby.workflows.definitions import PipelineDefinition, WorkflowDefinition

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.workflows.loader import WorkflowLoader

logger = logging.getLogger(__name__)


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
    workflow_type: str | None = None  # "step", "lifecycle", "pipeline"
    step_trace: list[WorkflowStepTrace] = field(default_factory=list)
    lifecycle_path: list[str] = field(default_factory=list)
    variables_declared: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "items": [i.to_dict() for i in self.items],
            "workflow_name": self.workflow_name,
            "workflow_type": self.workflow_type,
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


async def evaluate_workflow(
    name: str,
    workflow_loader: WorkflowLoader,
    project_path: str | None = None,
    mcp_manager: MCPClientManager | None = None,
) -> WorkflowEvaluation:
    """
    Evaluate a workflow definition for structural and semantic issues.

    Args:
        name: Workflow name to evaluate.
        workflow_loader: WorkflowLoader instance.
        project_path: Optional project path for resolution.
        mcp_manager: Optional MCPClientManager for semantic MCP tool checks.

    Returns:
        WorkflowEvaluation with findings.
    """
    result = WorkflowEvaluation(valid=True, workflow_name=name)

    # --- Phase A: Load & Basic Validation ---
    try:
        definition = await workflow_loader.load_workflow(name, project_path)
    except ValueError as e:
        result.valid = False
        result.items.append(
            EvaluationItem(
                layer="structure",
                level="error",
                code="WORKFLOW_LOAD_ERROR",
                message=f"Failed to load workflow '{name}': {e}",
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

    # Pipeline definitions get basic info only
    if isinstance(definition, PipelineDefinition):
        result.workflow_type = "pipeline"
        result.items.append(
            EvaluationItem(
                layer="structure",
                level="info",
                code="PIPELINE_TYPE",
                message=f"'{name}' is a pipeline workflow — step checks skipped",
            )
        )
        return result

    result.workflow_type = "enabled" if definition.enabled else "on-demand"

    # Always-on workflows get info notice
    if definition.enabled:
        result.items.append(
            EvaluationItem(
                layer="structure",
                level="info",
                code="ALWAYS_ON",
                message=f"'{name}' is an always-on workflow (enabled: true) — runs automatically on events",
            )
        )

    result.variables_declared = list(definition.variables.keys())

    # --- Phase B: Structural Validation ---
    _check_structure(definition, result)

    # --- Phase C: Semantic Validation ---
    await _check_semantics(definition, result, mcp_manager)

    # --- Step Trace Generation ---
    _build_step_trace(definition, result)

    # --- Lifecycle Path ---
    _build_lifecycle_path(definition, result)

    # Determine overall validity
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

    # Undefined transition targets
    for step in steps:
        for transition in step.transitions:
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

    # Dead-end steps (non-terminal steps with no transitions)
    exit_condition_names: set[str] = set()
    if definition.exit_condition:
        # Parse exit_condition for step name references (simple heuristic)
        for sn in step_names:
            if sn in definition.exit_condition:
                exit_condition_names.add(sn)

    last_step_name = step_names[-1] if step_names else None
    for step in steps:
        if (
            not step.transitions
            and step.name != last_step_name
            and step.name not in exit_condition_names
        ):
            result.items.append(
                EvaluationItem(
                    layer="structure",
                    level="warning",
                    code="DEAD_END_STEP",
                    message=f"Step '{step.name}' has no transitions and is not the final step",
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
    mcp_manager: MCPClientManager | None,
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
        # Check allowed_mcp_tools
        _check_mcp_tool_refs(
            step.name,
            "allowed_mcp_tools",
            step.allowed_mcp_tools,
            available_servers,
            server_tools,
            result,
        )
        # Check blocked_mcp_tools
        _check_mcp_tool_refs(
            step.name,
            "blocked_mcp_tools",
            step.blocked_mcp_tools,
            available_servers,
            server_tools,
            result,
        )

        # Check on_enter call_mcp_tool actions
        for action in step.on_enter:
            if isinstance(action, dict) and action.get("type") == "call_mcp_tool":
                server_name = action.get("server_name", "")
                tool_name = action.get("tool_name", "")
                if server_name and server_name not in available_servers:
                    result.items.append(
                        EvaluationItem(
                            layer="semantics",
                            level="warning",
                            code="UNKNOWN_MCP_ACTION_TARGET",
                            message=f"Step '{step.name}' on_enter calls unknown MCP server '{server_name}'",
                            detail={"step": step.name, "server": server_name, "tool": tool_name},
                        )
                    )
                elif (
                    server_name
                    and tool_name
                    and tool_name not in server_tools.get(server_name, set())
                ):
                    result.items.append(
                        EvaluationItem(
                            layer="semantics",
                            level="warning",
                            code="UNKNOWN_MCP_ACTION_TARGET",
                            message=f"Step '{step.name}' on_enter calls unknown tool '{server_name}:{tool_name}'",
                            detail={"step": step.name, "server": server_name, "tool": tool_name},
                        )
                    )

        for handler in [*step.on_mcp_success, *step.on_mcp_error]:
            _check_mcp_handler_ref(step.name, handler, available_servers, server_tools, result)


def _check_mcp_tool_refs(
    step_name: str,
    field_name: str,
    tools: list[str] | str,
    available_servers: set[str],
    server_tools: dict[str, set[str]],
    result: WorkflowEvaluation,
) -> None:
    """Check MCP tool references in allowed/blocked lists."""
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
                    level="warning",
                    code="UNKNOWN_MCP_SERVER",
                    message=f"Step '{step_name}' {field_name} references unknown server '{server}'",
                    detail={"step": step_name, "server": server, "ref": ref},
                )
            )
        elif tool and tool != "*" and tool not in server_tools.get(server, set()):
            result.items.append(
                EvaluationItem(
                    layer="semantics",
                    level="warning",
                    code="UNKNOWN_MCP_TOOL",
                    message=f"Step '{step_name}' {field_name} references unknown tool '{ref}'",
                    detail={"step": step_name, "server": server, "tool": tool, "ref": ref},
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
    elif tool != "*" and tool not in server_tools.get(server, set()):
        result.items.append(
            EvaluationItem(
                layer="semantics",
                level="warning",
                code="UNKNOWN_MCP_HANDLER_TARGET",
                message=f"Step '{step_name}' handler references unknown tool '{ref}'",
                detail={"step": step_name, "server": server, "tool": tool, "ref": ref},
            )
        )


def _build_step_trace(definition: WorkflowDefinition, result: WorkflowEvaluation) -> None:
    """Build step trace summaries for each step."""
    for step in definition.steps:
        # Summarize on_enter actions
        action_summaries: list[str] = []
        for action in step.on_enter:
            if isinstance(action, dict):
                action_type = action.get("type", "unknown")
                if action_type == "call_mcp_tool":
                    server = action.get("server_name", "?")
                    tool = action.get("tool_name", "?")
                    action_summaries.append(f"call_mcp_tool: {server}:{tool}")
                elif action_type == "set_variable":
                    var_name = action.get("name", "?")
                    action_summaries.append(f"set_variable: {var_name}")
                elif action_type == "inject_message":
                    action_summaries.append("inject_message")
                else:
                    action_summaries.append(action_type)

        # Summarize transitions
        transitions = [{"to": t.to, "when": t.when} for t in step.transitions]

        # Summarize on_mcp_success/error
        mcp_success: list[str] = []
        for handler in step.on_mcp_success:
            if isinstance(handler, dict):
                server = handler.get("server", "?")
                tool = handler.get("tool", "?")
                action = handler.get("action", "?")
                mcp_success.append(f"{server}:{tool} -> {action}")

        mcp_error: list[str] = []
        for handler in step.on_mcp_error:
            if isinstance(handler, dict):
                server = handler.get("server", "?")
                tool = handler.get("tool", "?")
                action = handler.get("action", "?")
                mcp_error.append(f"{server}:{tool} -> {action}")

        result.step_trace.append(
            WorkflowStepTrace(
                name=step.name,
                description=step.description,
                on_enter_actions=action_summaries,
                allowed_tools=step.allowed_tools,
                blocked_tools=step.blocked_tools,
                allowed_mcp_tools=step.allowed_mcp_tools,
                blocked_mcp_tools=step.blocked_mcp_tools,
                transitions=transitions,
                on_mcp_success=mcp_success,
                on_mcp_error=mcp_error,
            )
        )


def _build_lifecycle_path(definition: WorkflowDefinition, result: WorkflowEvaluation) -> None:
    """Build primary lifecycle path via first transitions."""
    if not definition.steps:
        return

    step_map = {s.name: s for s in definition.steps}
    path: list[str] = []
    visited: set[str] = set()
    current = definition.steps[0].name

    while current and current not in visited:
        visited.add(current)
        path.append(current)
        step = step_map.get(current)
        if step and step.transitions:
            current = step.transitions[0].to
        else:
            break

    result.lifecycle_path = path
