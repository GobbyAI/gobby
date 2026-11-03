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


def dev_pod_launch_refusal(
    sessions: SessionManager | None,
    caller_ref: Any,
    tool_name: Any,
    arguments: Any,
    pod_pipelines: Any,
) -> str | None:
    """Why a pod pipeline launch is refused, or None when the launch may proceed.

    A running epic manager may launch a pod pipeline for its own open leaf or relaunch one.
    ``pod_pipelines`` is the rule's YAML list of pipeline names. A create passes
    ``parent_team_id`` and no ``team_id``; a relaunch passes ``team_id``, optionally with the
    matching ``parent_team_id``.
    """
    from gobby.agents.team_admission_records import TeamAdmissionRefused
    from gobby.agents.team_lifecycle import pod_parent, pod_relaunch

    if sessions is None or not isinstance(pod_pipelines, list):
        return "pod launches need a session store and a list of pod pipelines"
    try:
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            return "the pipeline launch arguments are not an object"
        if tool_name == "run_pipeline":
            pipeline, inputs = arguments.get("name"), arguments.get("inputs")
        elif isinstance(tool_name, str) and tool_name.startswith("pipeline:"):
            pipeline, inputs = tool_name.removeprefix("pipeline:"), arguments
        else:
            return "only run_pipeline or a pipeline: launch starts a pod"
        if pipeline not in pod_pipelines:
            return f"pipeline {pipeline!r} is not a pod pipeline"
        if not isinstance(inputs, dict):
            return "the pod launch inputs are not an object"
        team_id, parent_id = inputs.get("team_id"), inputs.get("parent_team_id")
        task_ref = inputs.get("task")
        if not isinstance(task_ref, str) or any(
            value is not None and not isinstance(value, str) for value in (team_id, parent_id)
        ):
            return "a pod launch names its task and team ids as strings"
        caller, agent = _spawn_caller(sessions, caller_ref)
        if _running_team_manager_definition(sessions, caller, agent) is None:
            return "only a running team manager launches a pod"
        task_id = LocalTaskManager(sessions.db).resolve_task_reference(task_ref, caller.project_id)
        if team_id is not None:
            pod_relaunch(sessions.db, caller.id, task_id, team_id, parent_id)
        elif isinstance(parent_id, str):
            pod_parent(sessions.db, caller.id, task_id, parent_id)
        else:
            return "a pod create names the parent team with parent_team_id"
        return None
    except TeamAdmissionRefused as exc:
        return f"{exc.code}: {exc}"
    except (ValueError, TaskNotFoundError, AgentResolutionError) as exc:
        return str(exc) or "the pod launch task or caller did not resolve"
