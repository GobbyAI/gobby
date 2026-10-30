"""Dev Pod launch authority for the seat rules: an epic manager's own pods, named in YAML."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from gobby.storage.tasks import LocalTaskManager, TaskNotFoundError
from gobby.workflows.agent_resolver import AgentResolutionError
from gobby.workflows.condition_helpers_sessions import (
    _running_team_manager_definition,
    _spawn_caller,
)

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager


def dev_pod_launch_allowed(
    sessions: SessionManager | None,
    caller_ref: Any,
    tool_name: Any,
    arguments: Any,
    pod_pipelines: Any,
) -> bool:
    """A running epic manager may launch a pod pipeline for its own open leaf or relaunch one.

    ``pod_pipelines`` is the rule's YAML list of pipeline names. A create passes
    ``parent_team_id`` and no ``team_id``; a relaunch passes ``team_id``, optionally with the
    matching ``parent_team_id``, and may be a fresh reviewer.
    """
    from gobby.agents.team_lifecycle import TeamAdmissionRefused, pod_parent, pod_relaunch

    if sessions is None or not isinstance(pod_pipelines, list):
        return False
    try:
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            return False
        if tool_name == "run_pipeline":
            pipeline, inputs = arguments.get("name"), arguments.get("inputs")
        elif isinstance(tool_name, str) and tool_name.startswith("pipeline:"):
            pipeline, inputs = tool_name.removeprefix("pipeline:"), arguments
        else:
            return False
        if pipeline not in pod_pipelines or not isinstance(inputs, dict):
            return False
        team_id, parent_id = inputs.get("team_id"), inputs.get("parent_team_id")
        task_ref = inputs.get("task")
        if not isinstance(task_ref, str) or any(
            value is not None and not isinstance(value, str) for value in (team_id, parent_id)
        ):
            return False
        if team_id is None and inputs.get("fresh_reviewer"):
            return False
        caller, agent = _spawn_caller(sessions, caller_ref)
        if _running_team_manager_definition(sessions, caller, agent) is None:
            return False
        task_id = LocalTaskManager(sessions.db).resolve_task_reference(task_ref, caller.project_id)
        if team_id is not None:
            pod_relaunch(sessions.db, caller.id, task_id, team_id, parent_id)
        elif isinstance(parent_id, str):
            pod_parent(sessions.db, caller.id, task_id, parent_id)
        else:
            return False
        return True
    except (ValueError, TaskNotFoundError, TeamAdmissionRefused, AgentResolutionError):
        return False
