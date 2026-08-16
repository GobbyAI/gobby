"""Trace helpers extracted from workflow dry-run evaluation."""

from __future__ import annotations

from collections import deque

from gobby.workflows.definitions import WorkflowDefinition
from gobby.workflows.dry_run import WorkflowEvaluation, WorkflowStepTrace


def _build_step_trace(definition: WorkflowDefinition, result: WorkflowEvaluation) -> None:
    """Build step trace summaries for each step."""
    for step in definition.steps:
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

        transitions = [{"to": t.to, "when": t.when} for t in step.transitions]

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
    """List all reachable lifecycle steps in breadth-first transition order."""
    if not definition.steps:
        return

    step_map = {s.name: s for s in definition.steps}
    path: list[str] = []
    visited: set[str] = set()
    queue: deque[str] = deque([definition.steps[0].name])

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        path.append(current)
        step = step_map.get(current)
        if step:
            queue.extend(
                transition.to
                for transition in step.transitions
                if transition.to in step_map and transition.to not in visited
            )

    result.lifecycle_path = path
