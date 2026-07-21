"""Agent spawning through dispatcher-selected actions."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.skill_composition import inspect_skill_composition
from gobby.dispatch.spawn_artifacts import (
    SpawnIsolation,
    _artifact_ref_resolves,
    _artifact_ref_sha,
    _effective_spawn_isolation,
    _field,
    _guard_merge_ready_leaf_branch,
    _persist_spawn_artifacts,
    _prepare_spawn_artifacts,
    _project_clone_manager,
    _project_git_manager,
    _repair_leaf_target_branch,
    _sanitize_reusable_spawn_artifacts,
    _spawn_workspace_ids,
    ensure_epic_integration_workspaces,
)
from gobby.dispatch.spawn_completion import (
    BuildCompletionServices,
    _coordinator_session_matches_project,
    _subscribe_build_coordinator_completion,
    subscribe_agent_completion,
)
from gobby.dispatch.spawn_errors import DispatchSpawnFailed, DispatchSpawnUnavailable
from gobby.storage.hub.protocol import HubDatabase

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)

MAX_DISPATCH_SPAWN_ATTEMPTS = 3

__all__ = [
    "DispatchSpawnFailed",
    "DispatchSpawnUnavailable",
    "MAX_DISPATCH_SPAWN_ATTEMPTS",
    "SpawnIsolation",
    "_artifact_ref_resolves",
    "_artifact_ref_sha",
    "_coordinator_session_matches_project",
    "_effective_spawn_isolation",
    "_field",
    "_guard_merge_ready_leaf_branch",
    "_persist_spawn_artifacts",
    "_prepare_spawn_artifacts",
    "_project_clone_manager",
    "_project_git_manager",
    "_repair_leaf_target_branch",
    "_sanitize_reusable_spawn_artifacts",
    "_spawn_workspace_ids",
    "_subscribe_build_coordinator_completion",
    "ensure_epic_integration_workspaces",
    "spawn_agent",
    "subscribe_agent_completion",
]


async def spawn_agent(
    action: SpawnAgentAction,
    *,
    db: HubDatabase | None = None,
    context: object | None = None,
    services: object | None = None,
    mutex: object | None = None,
) -> str:
    """Spawn an agent through daemon services and return its real agent run id."""
    if db is None:
        raise DispatchSpawnUnavailable("database_missing")
    from gobby.agents.readiness import spawn_readiness_blocker

    readiness_reason = spawn_readiness_blocker(services)
    if readiness_reason is not None:
        logger.info("Dispatcher spawn skipped", extra={"readiness_reason": readiness_reason})
        raise DispatchSpawnUnavailable(readiness_reason)
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
    try:
        task = task_manager.get_task(action.task_id)
    except ValueError as err:
        raise DispatchSpawnFailed(f"task_not_found:{action.task_id}") from err
    if task is None:
        raise DispatchSpawnFailed(f"task_not_found:{action.task_id}")

    project_id = str(getattr(task, "project_id", "") or _field(context, "project_id", ""))
    if not project_id:
        raise DispatchSpawnFailed("project_id_missing")

    from gobby.agents.launcher_session import get_or_create_launcher_session
    from gobby.storage.projects import LocalProjectManager
    from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent

    try:
        agent_body = resolve_agent(action.agent_slug, db, project_id=project_id)
    except AgentResolutionError as exc:
        raise DispatchSpawnFailed(f"agent_definition_missing:{action.agent_slug}") from exc

    skill_composition = inspect_skill_composition(
        db,
        project_id=project_id,
        agent_body=agent_body,
        additional_skills=action.additional_skills,
    )
    if skill_composition.failure_reason is not None:
        raise DispatchSpawnFailed(skill_composition.failure_reason)

    parent_session_id = get_or_create_launcher_session(
        session_manager,
        project_id,
        "dispatcher_launcher",
        "Dispatcher Launcher",
    )

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
        if agent_body.steps:
            from gobby.mcp_proxy.tools.spawn_agent._factory import _register_agent_step_workflow

            initial_variables["_step_workflow_name"] = _register_agent_step_workflow(
                agent_body,
                db,
            )

    workflow = (
        agent_body.workflows.pipeline if agent_body and agent_body.workflows.pipeline else None
    )
    effective_isolation = _effective_spawn_isolation(
        task=task,
        action=action,
        agent_body=agent_body,
    )
    artifacts = await asyncio.to_thread(
        _prepare_spawn_artifacts,
        db=db,
        action=action,
        task=task,
        task_manager=task_manager,
        project_id=project_id,
        services=services,
        isolation=effective_isolation,
    )
    artifacts = await asyncio.to_thread(
        _sanitize_reusable_spawn_artifacts,
        db=db,
        task=task,
        artifacts=artifacts,
        services=services,
        isolation=effective_isolation,
    )
    artifacts = await asyncio.to_thread(
        _repair_leaf_target_branch,
        db=db,
        task=task,
        task_manager=task_manager,
        project_id=project_id,
        services=services,
        artifacts=artifacts,
        isolation=effective_isolation,
    )
    await asyncio.to_thread(
        _guard_merge_ready_leaf_branch,
        db=db,
        action=action,
        task=task,
        project_id=project_id,
        services=services,
        artifacts=artifacts,
        isolation=effective_isolation,
    )
    project = LocalProjectManager(db).get(project_id)
    project_path = project.repo_path if project is not None else None
    worktree_id, clone_id = _spawn_workspace_ids(
        task=task,
        action=action,
        artifacts=artifacts,
        isolation=effective_isolation,
    )

    from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

    result = await spawn_agent_impl(
        prompt=prompt,
        runner=runner,
        agent_body=agent_body,
        agent_lookup_name=action.agent_slug,
        task_id=action.task_id,
        task_manager=task_manager,
        isolation=effective_isolation,
        branch_name=None,
        base_branch=artifacts.target_branch,
        clone_id=clone_id,
        worktree_id=worktree_id,
        worktree_storage=getattr(services, "worktree_storage", None),
        git_manager=_project_git_manager(services, project_id),
        clone_storage=getattr(services, "clone_storage", None),
        clone_manager=_project_clone_manager(services, project_id),
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
        held_task_mutex=mutex,
    )
    if not result.get("success"):
        raise DispatchSpawnFailed(str(result.get("error") or "spawn_failed"))
    run_id = result.get("run_id")
    if not run_id:
        raise DispatchSpawnFailed("missing run_id")

    try:
        _persist_spawn_artifacts(db, action.task_id, result)
    except DispatchSpawnFailed as exc:
        raise DispatchSpawnFailed(
            str(exc),
            stage_failure_cited_subtasks=exc.stage_failure_cited_subtasks,
            spawned_run_id=str(run_id),
        ) from exc
    try:
        _subscribe_build_coordinator_completion(
            db=db,
            project_id=project_id,
            task_id=action.task_id,
            run_id=str(run_id),
            services=cast(BuildCompletionServices | None, services),
        )
    except Exception:
        logger.warning(
            "Failed to subscribe build coordinator to dispatcher-spawned agent completion",
            extra={"task_id": action.task_id, "run_id": str(run_id), "project_id": project_id},
            exc_info=True,
        )
    return str(run_id)
