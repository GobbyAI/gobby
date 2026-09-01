"""Agent spawning through dispatcher-selected actions."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.context import services_daemon_config
from gobby.dispatch.prompts import attach_plan_review_evidence
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
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import (
    CheckoutNotFoundError,
    CheckoutSentinelRejectedError,
    MissingMachineContextError,
    OverlayRegistrationRejectedError,
    require_root,
    resolve_operation_root,
)
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.workflows.definitions import AgentDefinitionBody

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


def _prepare_plan_adversary_evidence(
    *,
    db: HubDatabase,
    action: SpawnAgentAction,
    task: object,
    artifacts: object,
    project_id: str,
    prompt: str,
) -> tuple[str, PlanReviewEvidenceService | None, str | None]:
    if action.agent_slug != "plan-adversary":
        return prompt, None, None
    stage_name = str((action.initial_variables or {}).get("stage_name") or "")
    if stage_name != "planning":
        raise DispatchSpawnFailed("plan_review_stage_missing")
    plan_path = str(getattr(artifacts, "plan_file_path", "") or "")
    if not plan_path:
        raise DispatchSpawnFailed("plan_review_plan_path_missing")
    task_id = str(getattr(task, "id", "") or action.task_id)
    stage = next(
        (
            candidate
            for candidate in (getattr(task, "stages", ()) or ())
            if str(_field(candidate, "stage_name", "") or "") == stage_name
        ),
        None,
    )
    if stage is None:
        raise DispatchSpawnFailed("plan_review_stage_missing")
    review_round_count = _field(stage, "review_round_count", 0)
    if not isinstance(review_round_count, int) or isinstance(review_round_count, bool):
        raise DispatchSpawnFailed("plan_review_round_invalid")
    round_number = review_round_count + 1
    service = PlanReviewEvidenceService(db)
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=round_number,
        task_id=task_id,
        stage=stage_name,
    )
    try:
        transport = attach_plan_review_evidence(
            prompt,
            evidence_id=prepared.evidence_id,
            round_number=round_number,
        )
    except BaseException:
        _expire_failed_adversary_spawn(service, prepared.evidence_id)
        raise
    return transport, service, prepared.evidence_id


# Checkout resolution failures are terminal for the spawn: retrying the heartbeat
# cannot register a checkout, so they escalate as `DispatchSpawnFailed`.
_CHECKOUT_RESOLUTION_ERRORS: tuple[type[Exception], ...] = (
    CheckoutNotFoundError,
    OverlayRegistrationRejectedError,
    CheckoutSentinelRejectedError,
    MissingMachineContextError,
    MachineOwnershipMismatchError,
)


def _spawn_operation_root(db: HubDatabase, project_id: str, artifacts: object) -> str:
    machine_id = require_local_machine_id(
        None, resource_kind="project_checkout", resource_id=project_id
    )
    overlay = getattr(artifacts, "worktree_path", None) or getattr(artifacts, "clone_path", None)
    if overlay:
        return resolve_operation_root(db, project_id, machine_id, overlay_path=str(overlay))
    return require_root(db, project_id, machine_id)


async def _resolve_spawn_operation_root(db: HubDatabase, project_id: str, artifacts: object) -> str:
    """Resolve the spawn root off the event loop; an unresolved checkout is terminal."""
    try:
        return await asyncio.to_thread(_spawn_operation_root, db, project_id, artifacts)
    except _CHECKOUT_RESOLUTION_ERRORS as exc:
        raise DispatchSpawnFailed(f"checkout_unresolved:{exc}") from exc


def _expire_failed_adversary_spawn(
    service: PlanReviewEvidenceService | None,
    evidence_id: str | None,
) -> None:
    if service is None or evidence_id is None:
        return
    try:
        service.expire_plan_review_evidence(evidence_id, spawn_failed=True)
    except Exception:
        logger.warning(
            "Failed to expire prepared plan-review evidence",
            extra={"evidence_id": evidence_id},
            exc_info=True,
        )


def _with_skill_allowed_tools(
    agent_body: AgentDefinitionBody | None,
    allowed_tools: tuple[str, ...],
) -> AgentDefinitionBody | None:
    """Return an agent definition whose restricted steps include composed skill tools."""
    if agent_body is None or agent_body.step_workflow is None or not allowed_tools:
        return agent_body

    composed = agent_body.model_copy(deep=True)
    assert composed.step_workflow is not None
    for step in composed.step_workflow.steps:
        if step.allowed_tools == "all":
            continue
        step.allowed_tools = list(dict.fromkeys((*step.allowed_tools, *allowed_tools)))
    return composed


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
    from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent

    try:
        agent_body = await asyncio.to_thread(
            resolve_agent,
            action.agent_slug,
            db,
            project_id=project_id,
        )
    except AgentResolutionError as exc:
        raise DispatchSpawnFailed(f"agent_definition_missing:{action.agent_slug}") from exc

    skill_composition = await asyncio.to_thread(
        inspect_skill_composition,
        db,
        project_id=project_id,
        agent_body=agent_body,
        additional_skills=action.additional_skills,
    )
    if skill_composition.failure_reason is not None:
        raise DispatchSpawnFailed(skill_composition.failure_reason)
    agent_body = _with_skill_allowed_tools(agent_body, skill_composition.allowed_tools)

    parent_session_id = get_or_create_launcher_session(
        session_manager,
        project_id,
        "dispatcher_launcher",
    )

    prompt = action.prompt
    if agent_body is not None:
        try:
            preamble = agent_body.prompt_for("agent")
        except ValueError as exc:
            raise DispatchSpawnFailed(f"agent_surface_invalid:{exc}") from exc
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
    project_path = await _resolve_spawn_operation_root(db, project_id, artifacts)
    worktree_id, clone_id = _spawn_workspace_ids(
        task=task,
        action=action,
        artifacts=artifacts,
        isolation=effective_isolation,
    )
    prompt, evidence_service, evidence_id = await asyncio.to_thread(
        _prepare_plan_adversary_evidence,
        db=db,
        action=action,
        task=task,
        artifacts=artifacts,
        project_id=project_id,
        prompt=prompt,
    )

    from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

    try:
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
            completion_registry=getattr(services, "completion_registry", None),
            daemon_config=services_daemon_config(services),
            code_index=getattr(services, "code_indexer", None),
            held_task_mutex=mutex,
            terminal_backend=action.terminal_backend,
        )
    except BaseException:
        _expire_failed_adversary_spawn(evidence_service, evidence_id)
        raise
    if not result.get("success"):
        _expire_failed_adversary_spawn(evidence_service, evidence_id)
        raise DispatchSpawnFailed(str(result.get("error") or "spawn_failed"))
    run_id = result.get("run_id")
    if not run_id:
        _expire_failed_adversary_spawn(evidence_service, evidence_id)
        raise DispatchSpawnFailed("missing run_id")
    if evidence_service is not None and evidence_id is not None:
        try:
            evidence_service.bind_evidence_run(evidence_id, str(run_id))
        except ReviewEvidenceError as exc:
            _expire_failed_adversary_spawn(evidence_service, evidence_id)
            raise DispatchSpawnFailed(
                f"plan_review_evidence_bind_failed:{exc.code}",
                spawned_run_id=str(run_id),
            ) from exc
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
