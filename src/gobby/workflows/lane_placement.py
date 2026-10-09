"""Lane placement for new tasks (#23814).

In a project with an open epic labeled ``lane``, every new task belongs inside a
lane: its parent chain must reach an open lane epic. Rule conditions cannot walk
parents, so the bundled ``refuse-task-outside-lane`` rule calls these helpers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError

logger = logging.getLogger(__name__)

LANE_LABEL = "lane"
# A longer chain is cyclic or corrupt; it reaches no lane.
_MAX_CHAIN_DEPTH = 64
_CALL_TOOL_NAMES = frozenset({"call_tool", "mcp__gobby__call_tool"})


def open_lane_epics(task_manager: LocalTaskManager, project_id: str) -> list[Task]:
    """Return the project's open epics labeled ``lane``."""
    return task_manager.list_tasks(
        project_id=project_id, closed=False, task_type="epic", label=LANE_LABEL, limit=100
    )


def open_lane_epic_refs(task_manager: LocalTaskManager, project_id: str | None) -> str:
    """Name the project's open lane epics for a refusal message."""
    if not project_id:
        return ""
    return ", ".join(
        f"#{epic.seq_num} {epic.title}" for epic in open_lane_epics(task_manager, project_id)
    )


def creates_task_outside_lane(
    task_manager: LocalTaskManager,
    session_project_id: str | None,
    tool_input: Any,
    event_data: Any,
) -> bool:
    """Return whether a task-creating MCP call would create a task outside every lane.

    Covers ``gobby-tasks:create_task``, expansion under a target task
    (``gobby-tasks-ops:start_expansion_run``) and plan import
    (``gobby-tasks-ops:build_task`` with a plan file, which creates an
    unparented epic). A project with no open lane epic is unaffected. A target
    project or parent that does not resolve is left to the tool, which refuses
    it and writes nothing.
    """
    if not isinstance(tool_input, Mapping):
        return False
    data = event_data if isinstance(event_data, Mapping) else {}
    server, tool = _target_tool(tool_input, data)
    if server == "gobby-tasks" and tool == "create_task":
        project_id = _project_id(task_manager, tool_input.get("project"), session_project_id)
        if not project_id or not open_lane_epics(task_manager, project_id):
            return False
        parent_ref = tool_input.get("parent_task_id")
        if not parent_ref:
            labels = tool_input.get("labels")
            is_lane = isinstance(labels, list) and LANE_LABEL in labels
            return not (tool_input.get("task_type") == "epic" and is_lane)
        parent = _resolve_task(task_manager, str(parent_ref), project_id)
        return parent is not None and not _reaches_open_lane_epic(task_manager, parent)
    if server == "gobby-tasks-ops" and tool == "start_expansion_run":
        project_id = _project_id(task_manager, tool_input.get("project"), session_project_id)
        target_ref = tool_input.get("task_id")
        if not project_id or not target_ref:
            return False
        target = _resolve_task(task_manager, str(target_ref), project_id)
        if target is None or not open_lane_epics(task_manager, target.project_id):
            return False
        return not _reaches_open_lane_epic(task_manager, target)
    if server == "gobby-tasks-ops" and tool == "build_task":
        from gobby.build.input_resolution import looks_like_task_ref

        input_ref = tool_input.get("input_ref")
        if not isinstance(input_ref, str) or not input_ref or looks_like_task_ref(input_ref):
            return False
        project_id = _project_id(task_manager, tool_input.get("project_id"), session_project_id)
        return bool(project_id) and bool(open_lane_epics(task_manager, str(project_id)))
    return False


def _target_tool(tool_input: Mapping[str, Any], data: Mapping[str, Any]) -> tuple[str, str]:
    server = data.get("mcp_server")
    tool = data.get("mcp_tool")
    if not (server and tool) and data.get("tool_name") in _CALL_TOOL_NAMES:
        server = tool_input.get("server_name") or tool_input.get("server")
        tool = tool_input.get("tool_name") or tool_input.get("tool")
    return str(server or ""), str(tool or "")


def _project_id(
    task_manager: LocalTaskManager, project_ref: Any, session_project_id: str | None
) -> str | None:
    if isinstance(project_ref, str) and project_ref:
        project = LocalProjectManager(task_manager.db).resolve_ref(project_ref)
        return project.id if project else None
    if session_project_id in (None, "", "unknown"):
        return None
    return session_project_id


def _resolve_task(task_manager: LocalTaskManager, ref: str, project_id: str) -> Task | None:
    from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp

    try:
        return task_manager.get_task(resolve_task_id_for_mcp(task_manager, ref, project_id))
    except (TaskNotFoundError, ValueError):
        logger.debug("Lane placement: task %r does not resolve in %s", ref, project_id)
        return None


def _reaches_open_lane_epic(task_manager: LocalTaskManager, task: Task) -> bool:
    current: Task | None = task
    seen: set[str] = set()
    while current is not None and current.id not in seen and len(seen) < _MAX_CHAIN_DEPTH:
        if (
            current.task_type == "epic"
            and current.closed_at is None
            and LANE_LABEL in (current.labels or [])
        ):
            return True
        seen.add(current.id)
        parent_id = current.parent_task_id
        try:
            current = task_manager.get_task(parent_id) if parent_id else None
        except (TaskNotFoundError, ValueError):
            current = None
    return False
