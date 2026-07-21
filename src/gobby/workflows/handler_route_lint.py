"""Static checks for MCP handler failure routes and ordering guards."""

from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass
from typing import Any

from gobby.workflows.definitions import WorkflowDefinition, WorkflowStep
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator


@dataclass(frozen=True)
class HandlerRouteFinding:
    """A warning produced by static MCP handler-route analysis."""

    code: str
    message: str
    detail: dict[str, Any]


def check_handler_routes(definition: WorkflowDefinition) -> list[HandlerRouteFinding]:
    """Return missing failure-route and unsatisfiable ordering warnings."""
    reachable_steps = _reachable_steps(definition)
    declared = set(definition.variables)
    success_assignments = _assigned_variables(reachable_steps, "on_mcp_success")
    error_assignments = _assigned_variables(reachable_steps, "on_mcp_error")
    before_assignments = _assigned_variables(reachable_steps, "on_mcp_before")
    known_variables = declared | success_assignments | error_assignments | before_assignments
    success_only = success_assignments - error_assignments - before_assignments

    findings: list[HandlerRouteFinding] = []
    for step in definition.steps:
        success_targets = {_handler_target(handler) for handler in step.on_mcp_success}
        error_targets = {_handler_target(handler) for handler in step.on_mcp_error}
        for server, tool in sorted(success_targets - error_targets):
            if server and tool:
                findings.append(
                    HandlerRouteFinding(
                        code="MISSING_FAILURE_ROUTE",
                        message=(
                            f"Step '{step.name}' handles success for MCP tool "
                            f"'{server}:{tool}' without an on_mcp_error route"
                        ),
                        detail={"step": step.name, "server": server, "tool": tool},
                    )
                )

        for route_name, handlers in (
            ("on_mcp_success", step.on_mcp_success),
            ("on_mcp_error", step.on_mcp_error),
        ):
            for handler in handlers:
                findings.extend(
                    _ordering_findings(
                        step,
                        route_name,
                        handler,
                        known_variables,
                        success_only,
                        definition.variables,
                    )
                )
    return findings


def _ordering_findings(
    step: WorkflowStep,
    route_name: str,
    handler: dict[str, Any],
    known_variables: set[str],
    success_only: set[str],
    defaults: dict[str, Any],
) -> list[HandlerRouteFinding]:
    condition = handler.get("when")
    if not isinstance(condition, str):
        return []

    target = _handler_target(handler)
    referenced = _condition_variables(condition)
    missing = sorted(referenced - known_variables)
    if missing:
        return [
            _ordering_finding(
                step,
                target,
                condition,
                missing,
                "no reachable declaration or handler assignment",
            )
        ]

    success_dependencies = sorted(referenced & success_only)
    if (
        route_name == "on_mcp_error"
        and success_dependencies
        and _false_with_defaults(condition, defaults)
    ):
        return [
            _ordering_finding(
                step,
                target,
                condition,
                success_dependencies,
                "assigned only by success handlers",
            )
        ]
    return []


def _ordering_finding(
    step: WorkflowStep,
    target: tuple[str, str],
    condition: str,
    variables: list[str],
    reason: str,
) -> HandlerRouteFinding:
    server, tool = target
    return HandlerRouteFinding(
        code="ORDERING_VAR_UNSATISFIABLE",
        message=(
            f"Step '{step.name}' handler for '{server}:{tool}' guards on variables "
            f"{reason}: {variables}"
        ),
        detail={
            "step": step.name,
            "server": server,
            "tool": tool,
            "condition": condition,
            "variables": variables,
            "reason": reason,
        },
    )


def _reachable_steps(definition: WorkflowDefinition) -> list[WorkflowStep]:
    if not definition.steps:
        return []
    by_name = {step.name: step for step in definition.steps}
    queue: deque[str] = deque([definition.steps[0].name])
    seen: set[str] = set()
    while queue:
        name = queue.popleft()
        if name in seen or name not in by_name:
            continue
        seen.add(name)
        queue.extend(transition.to for transition in by_name[name].transitions)
    return [step for step in definition.steps if step.name in seen]


def _assigned_variables(steps: list[WorkflowStep], route_name: str) -> set[str]:
    assigned: set[str] = set()
    for step in steps:
        for handler in getattr(step, route_name):
            variable = handler.get("variable")
            if handler.get("action") == "set_variable" and isinstance(variable, str):
                assigned.add(variable)
    return assigned


def _handler_target(handler: dict[str, Any]) -> tuple[str, str]:
    server = handler.get("server")
    tool = handler.get("tool")
    return (server if isinstance(server, str) else "", tool if isinstance(tool, str) else "")


def _condition_variables(condition: str) -> set[str]:
    try:
        tree = ast.parse(condition, mode="eval")
    except SyntaxError:
        return set()

    variables: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "vars"
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            variables.add(node.args[0].value)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "vars"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            variables.add(node.slice.value)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "vars"
            and node.attr != "get"
        ):
            variables.add(node.attr)
    return variables


def _false_with_defaults(condition: str, defaults: dict[str, Any]) -> bool:
    try:
        return not SafeExpressionEvaluator(
            {"vars": defaults, "tool_input": {}, "tool_output": {}}, {}
        ).evaluate(condition)
    except ValueError:
        return False
