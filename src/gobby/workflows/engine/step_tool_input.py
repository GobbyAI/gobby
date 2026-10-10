"""Inner MCP arguments and task identity exposed to step conditions."""

from __future__ import annotations

import json
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, TaskNotFoundError
from gobby.utils.project_context import get_project_context


def inner_tool_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Keep proxy routing keys separate from the target's arguments."""
    if "arguments" not in tool_input and "args" not in tool_input:
        return {} if "server_name" in tool_input and "tool_name" in tool_input else dict(tool_input)
    if tool_input.get("arguments") is not None and tool_input.get("args") is not None:
        return {}
    arguments = tool_input.get("arguments")
    if arguments is None:
        arguments = tool_input.get("args")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return {}
    return dict(arguments) if isinstance(arguments, dict) else {}


def step_tool_input(
    tool_input: dict[str, Any], db: HubDatabase | None, project_id: str | None = None
) -> dict[str, Any]:
    """Resolve scoped task references in the same project as the task tool.

    Provider hooks precede request context seeding, so an explicit wrapper project
    takes precedence. Proxy hooks already have the seeded target project context.
    UUIDs carry their own identity and need no project-dependent normalization.
    """
    arguments = inner_tool_input(tool_input)
    task_ref = arguments.get("task_id")
    if (
        db is None
        or tool_input.get("server_name") != "gobby-tasks"
        or not isinstance(task_ref, str)
    ):
        return arguments
    if not (task_ref.startswith("#") or task_ref.isdigit() or "." in task_ref):
        return arguments
    context = get_project_context()
    project_ref = tool_input.get("project_id") or (context or {}).get("id") or project_id
    project = (
        LocalProjectManager(db).resolve_ref(project_ref)
        if isinstance(project_ref, str) and project_ref
        else None
    )
    if project is None:
        return arguments
    try:
        arguments["task_id"] = LocalTaskManager(db).resolve_task_reference(task_ref, project.id)
    except TaskNotFoundError:
        # An unresolved reference cannot mutate a task; preserve the tool's error input.
        pass
    return arguments
