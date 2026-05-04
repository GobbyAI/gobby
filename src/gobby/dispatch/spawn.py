"""Daemon-backed spawn boundary for dispatcher actions."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from gobby.dispatch.actions import SpawnAgentAction
from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._artifacts import TaskArtifactConstraintError, TaskArtifactManager
from gobby.storage.tasks._artifacts import set_artifacts_atomic as _set_artifacts_atomic

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)

MAX_DISPATCH_SPAWN_ATTEMPTS = 3


class DispatchSpawnUnavailable(RuntimeError):
    """Raised when dispatcher lacks the daemon services needed to spawn."""


class DispatchSpawnFailed(RuntimeError):
    """Raised when the daemon spawn path returns an unsuccessful result."""


async def spawn_agent(
    action: SpawnAgentAction,
    *,
    db: DatabaseProtocol | None = None,
    context: object | None = None,
    services: object | None = None,
) -> str:
    """Spawn an agent through daemon services and return its real agent run id."""
    if db is None:
        raise DispatchSpawnUnavailable("database_missing")
    required = {
        "database": getattr(services, "database", None),
        "task_manager": getattr(services, "task_manager", None),
        "session_manager": getattr(services, "session_manager", None),
        "agent_runner": getattr(services, "agent_runner", None),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise DispatchSpawnUnavailable(f"services_missing:{','.join(missing)}")

    task_manager = cast("LocalTaskManager", required["task_manager"])
    session_manager = cast("SessionManager", required["session_manager"])
    runner = cast("AgentRunner", required["agent_runner"])
    task = task_manager.get_task(action.task_id)
    if task is None:
        raise DispatchSpawnFailed(f"task_not_found:{action.task_ref}")

    project_id = str(getattr(task, "project_id", "") or _field(context, "project_id", ""))
    if not project_id:
        raise DispatchSpawnFailed("project_id_missing")

    from gobby.agents.launcher_session import get_or_create_launcher_session
    from gobby.storage.projects import LocalProjectManager
    from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent

    parent_session_id = get_or_create_launcher_session(
        session_manager,
        project_id,
        "dispatcher_launcher",
        "Dispatcher Launcher",
    )
    try:
        agent_body = resolve_agent(action.agent_slug, db, project_id=project_id)
    except AgentResolutionError as exc:
        raise DispatchSpawnFailed(f"agent_definition_missing:{action.agent_slug}") from exc

    prompt = action.prompt
    if agent_body is not None:
        preamble = agent_body.build_prompt_preamble()
        if preamble:
            prompt = f"{preamble}\n\n---\n\n{prompt}"

    initial_variables = dict(action.initial_variables or {})
    initial_variables["_agent_type"] = action.agent_slug
    if action.additional_skills:
        initial_variables["additional_skills"] = list(action.additional_skills)
    if agent_body is not None:
        if agent_body.workflows.rules:
            initial_variables["_agent_rules"] = agent_body.workflows.rules
        if agent_body.workflows.variables:
            initial_variables.update(agent_body.workflows.variables)

    workflow = (
        agent_body.workflows.pipeline if agent_body and agent_body.workflows.pipeline else None
    )
    artifacts = TaskArtifactManager(db).get_artifacts(action.task_id)
    project = LocalProjectManager(db).get(project_id)
    project_path = project.repo_path if project is not None else None

    from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

    result = await spawn_agent_impl(
        prompt=prompt,
        runner=runner,
        agent_body=agent_body,
        agent_lookup_name=action.agent_slug,
        task_id=action.task_ref,
        task_manager=task_manager,
        isolation=getattr(task, "isolation", None),
        branch_name=None,
        base_branch=artifacts.target_branch,
        clone_id=artifacts.clone_id,
        worktree_id=artifacts.worktree_id,
        worktree_storage=getattr(services, "worktree_storage", None),
        git_manager=getattr(services, "git_manager", None)
        or _service_git_manager(services, project_id),
        clone_storage=getattr(services, "clone_storage", None),
        clone_manager=getattr(services, "clone_manager", None),
        workflow=workflow,
        provider=None,
        model=action.model_override,
        reasoning_effort=action.reasoning_effort,
        parent_session_id=parent_session_id,
        project_path=project_path,
        initial_variables=initial_variables,
        session_manager=session_manager,
        db=db,
        daemon_config=getattr(services, "config", None),
        code_index=getattr(services, "code_indexer", None),
    )
    if not result.get("success"):
        raise DispatchSpawnFailed(str(result.get("error") or "spawn_failed"))
    run_id = result.get("run_id")
    if not run_id:
        raise DispatchSpawnFailed("missing run_id")

    _persist_spawn_artifacts(db, action.task_id, result)
    return str(run_id)


def _service_git_manager(services: object | None, project_id: str) -> object | None:
    getter = getattr(services, "get_git_manager", None)
    if callable(getter):
        return cast(object | None, getter(project_id))
    return None


def _persist_spawn_artifacts(
    db: DatabaseProtocol,
    task_id: str,
    result: Mapping[str, object],
) -> None:
    fields: dict[str, str | int | None] = {}
    worktree_id = result.get("worktree_id")
    worktree_path = result.get("worktree_path")
    clone_id = result.get("clone_id")
    if isinstance(worktree_id, str) and isinstance(worktree_path, str):
        fields["worktree_id"] = worktree_id
        fields["worktree_path"] = worktree_path
    clone_path = result.get("clone_path")
    if isinstance(clone_id, str) and isinstance(clone_path, str):
        fields["clone_id"] = clone_id
        fields["clone_path"] = clone_path
    if fields:
        try:
            _set_artifacts_atomic(db, task_id, **fields)
        except (TaskArtifactConstraintError, ValueError, sqlite3.DatabaseError):
            logger.warning("Failed to persist dispatcher spawn artifacts", exc_info=True)


def _field(
    obj: object | None,
    name: str,
    default: object | None = None,
) -> object | None:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return cast(object | None, obj.get(name, default))
    return cast(object | None, getattr(obj, name, default))
