"""Task-scoped build lifecycle controls."""

from __future__ import annotations

import logging

import gobby.build.control_artifacts as control_artifacts
import gobby.build.control_runtime as control_runtime
import gobby.build.restart_controls as restart_controls
import gobby.build.results as build_results
from gobby.build.branch_cleanup import delete_orphan_build_branches
from gobby.build.dispatch_tick import kick_dispatcher_tick as _kick_dispatcher_tick
from gobby.build.options import BuildOptions
from gobby.build.project_controls import build_resume as _resume_project_automation
from gobby.build.project_state import is_project_automation_enabled
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


async def build_stop_target(
    input_ref: str,
    *,
    db: HubDatabase,
    project_id: str,
    services: object | None = None,
) -> build_results.BuildTargetControlResult:
    """Stop automation for a single task or epic subtree."""
    task_manager = LocalTaskManager(db)
    root = control_runtime._resolve_task_ref(task_manager, input_ref, project_id)
    tasks = control_runtime._affected_tasks(task_manager, root)
    task_ids = [task.id for task in tasks]
    agents = control_runtime._active_agents(db, task_ids)
    parked = control_runtime._parked_daemon_stop_runs(db, task_ids)

    updated = 0
    for task in tasks:
        task_manager.update_task(task.id, allow_automation=False)
        updated += 1

    await control_runtime._cancel_active_agents(db, agents, services=services)
    parked_runs_released = await control_runtime._give_up_parked_daemon_stop_runs(
        db,
        parked,
        services=services,
    )
    mutexes_cleared = control_runtime._clear_dispatch_mutexes(db, task_ids)
    claims_released = control_runtime._release_stale_agent_claims(task_manager, db, tasks)
    stages_reset = control_runtime._reset_stoppable_stages(
        db,
        tasks,
        reason="build_stop",
    )

    result = build_results.BuildTargetControlResult(
        action="stop",
        project_id=project_id,
        root_task_id=root.id,
        affected_tasks=control_runtime._task_summaries(tasks),
        agents=control_runtime._agent_summaries(agents),
        automation_updated=updated,
        mutexes_cleared=mutexes_cleared,
        claims_released=claims_released,
        parked_runs_released=parked_runs_released,
        stages_reset=stages_reset,
    )
    build_results._record_target_history(db, result, input_ref=input_ref)
    return result


async def build_resume_target(
    input_ref: str,
    *,
    db: HubDatabase,
    project_id: str,
    services: object | None = None,
) -> build_results.BuildTargetControlResult:
    """Resume automation for a single task or epic subtree."""
    task_manager = LocalTaskManager(db)
    root = control_runtime._resolve_task_ref(task_manager, input_ref, project_id)
    tasks = control_runtime._affected_tasks(task_manager, root)
    task_ids = [task.id for task in tasks]

    updated = 0
    for task in tasks:
        task_manager.update_task(task.id, allow_automation=True)
        updated += 1

    mutexes_cleared = control_runtime._clear_stale_dispatch_mutexes(db, task_ids)
    claims_released = control_runtime._release_stale_agent_claims(task_manager, db, tasks)
    if not is_project_automation_enabled(db, project_id):
        _resume_project_automation(db=db, project_id=project_id)
    tick = await _kick_dispatcher_tick(db, project_id, services=services)

    result = build_results.BuildTargetControlResult(
        action="resume",
        project_id=project_id,
        root_task_id=root.id,
        affected_tasks=control_runtime._task_summaries(tasks),
        automation_updated=updated,
        mutexes_cleared=mutexes_cleared,
        claims_released=claims_released,
        dispatcher_tick=tick,
    )
    build_results._record_target_history(db, result, input_ref=input_ref)
    return result


async def build_clean_target(
    input_ref: str,
    *,
    db: HubDatabase,
    project_id: str,
    dry_run: bool = False,
    force: bool = False,
    delete_dirty_worktrees: bool = False,
    yes: bool = False,
    services: object | None = None,
) -> build_results.BuildTargetControlResult:
    """Delete failed build artifacts for a single task or epic subtree."""
    if not dry_run and not yes:
        raise ValueError("clean is destructive; pass yes=True to confirm")

    task_manager = LocalTaskManager(db)
    root = control_runtime._resolve_task_ref(task_manager, input_ref, project_id)
    tasks = control_runtime._affected_tasks(task_manager, root)
    task_ids = [task.id for task in tasks]
    agents = control_runtime._active_agents(db, task_ids)
    artifacts = control_artifacts.collect_clean_artifacts(db, project_id, tasks)
    blocked = control_runtime._clean_blockers(tasks, agents, force=force)

    if dry_run:
        result = build_results.BuildTargetControlResult(
            action="clean",
            project_id=project_id,
            root_task_id=root.id,
            affected_tasks=control_runtime._task_summaries(tasks),
            agents=control_runtime._agent_summaries(agents),
            artifacts=artifacts,
            dry_run=True,
            force=force,
            blocked_reasons=blocked,
        )
        build_results._record_target_history(db, result, input_ref=input_ref)
        return result

    if blocked:
        raise ValueError("; ".join(blocked))

    if force and agents:
        await control_runtime._cancel_active_agents(db, agents, services=services)

    if delete_dirty_worktrees:
        artifacts_to_delete = artifacts
    else:
        artifacts_to_delete = control_artifacts.classify_dirty_descendant_worktree_artifacts(
            db,
            artifacts,
            root=root,
            tasks=tasks,
            project_path=control_artifacts.get_project_path(db, project_id),
        )
    control_artifacts.delete_artifacts(db, project_id, artifacts_to_delete, force=force)
    delete_errors = [artifact.error for artifact in artifacts if artifact.error]
    if any(artifact.deferred for artifact in artifacts):
        branches_deleted = 0
        branch_errors: list[str] = []
    else:
        branches_deleted, branch_errors = delete_orphan_build_branches(
            db,
            project_id,
            tasks,
        )
    cleanup_errors = [*delete_errors, *branch_errors]
    if cleanup_errors:
        raise ValueError("; ".join(cleanup_errors))

    mutexes_cleared = control_runtime._clear_dispatch_mutexes(db, task_ids)
    claims_released = control_runtime._release_stale_agent_claims(task_manager, db, tasks)
    stages_reset = control_runtime._reset_current_stages(db, tasks, reason="build_clean")

    result = build_results.BuildTargetControlResult(
        action="clean",
        project_id=project_id,
        root_task_id=root.id,
        affected_tasks=control_runtime._task_summaries(tasks),
        agents=control_runtime._agent_summaries(agents),
        artifacts=artifacts,
        force=force,
        mutexes_cleared=mutexes_cleared,
        claims_released=claims_released,
        stages_reset=stages_reset,
        branches_deleted=branches_deleted,
    )
    build_results._record_target_history(db, result, input_ref=input_ref)
    return result


def cleanup_successful_merge_artifacts(
    db: HubDatabase,
    task_id: str,
    *,
    project_id: str | None = None,
    preserve_worktree_ids: set[str] | None = None,
) -> list[control_artifacts.BuildArtifactSummary]:
    """Best-effort cleanup for build artifacts after a merge stage succeeds."""
    task_manager = LocalTaskManager(db)
    root = task_manager.get_task(task_id, project_id=project_id)
    cleanup_project_id = project_id or root.project_id
    tasks = control_runtime._affected_tasks(task_manager, root)
    artifacts = control_artifacts.collect_clean_artifacts(db, cleanup_project_id, tasks)
    if not artifacts:
        return []

    active_agents = control_runtime._active_agents(db, [task.id for task in tasks])
    artifacts_to_delete = control_artifacts.defer_active_agent_artifacts(
        artifacts,
        active_agents,
    )
    if preserve_worktree_ids:
        artifacts_to_delete = control_artifacts.defer_preserved_worktree_artifacts(
            artifacts_to_delete,
            preserve_worktree_ids,
        )
    artifacts_to_delete = control_artifacts.classify_dirty_descendant_worktree_artifacts(
        db,
        artifacts_to_delete,
        root=root,
        tasks=tasks,
        project_path=control_artifacts.get_project_path(db, cleanup_project_id),
    )

    control_artifacts.delete_artifacts(
        db,
        cleanup_project_id,
        artifacts_to_delete,
        force=True,
    )
    if any(artifact.deferred for artifact in artifacts):
        _branches_deleted = 0
        branch_errors: list[str] = []
    else:
        _branches_deleted, branch_errors = delete_orphan_build_branches(
            db,
            cleanup_project_id,
            tasks,
        )
    errors = [artifact.error for artifact in artifacts if artifact.error] + branch_errors
    if errors:
        logger.warning(
            "successful_build_cleanup_incomplete",
            extra={
                "task_id": task_id,
                "project_id": cleanup_project_id,
                "errors": errors,
            },
        )
    else:
        logger.info(
            "successful_build_cleanup_completed",
            extra={
                "task_id": task_id,
                "project_id": cleanup_project_id,
                "artifacts_deleted": len([artifact for artifact in artifacts if artifact.deleted]),
                "artifacts_deferred": len(
                    [artifact for artifact in artifacts if artifact.deferred]
                ),
            },
        )
    return artifacts


async def build_restart_target(
    input_ref: str,
    *,
    db: HubDatabase,
    project_id: str,
    dry_run: bool = False,
    force: bool = False,
    yes: bool = False,
    no_resume: bool = False,
    opts: BuildOptions | None = None,
    services: object | None = None,
) -> build_results.BuildTargetControlResult:
    """Stop, clean, and resume automation for a task or epic subtree."""
    if not dry_run and not yes:
        raise ValueError("restart is destructive; pass yes=True to confirm")

    if dry_run:
        preview = await build_clean_target(
            input_ref,
            db=db,
            project_id=project_id,
            dry_run=True,
            force=force,
            yes=True,
            services=services,
        )
        preview.action = "restart"
        build_results._record_target_history(db, preview, input_ref=input_ref)
        return preview

    stop_result = await build_stop_target(
        input_ref, db=db, project_id=project_id, services=services
    )
    clean_result = await build_clean_target(
        input_ref,
        db=db,
        project_id=project_id,
        dry_run=False,
        force=force,
        yes=True,
        services=services,
    )
    task_manager = LocalTaskManager(db)
    root = control_runtime._resolve_task_ref(task_manager, input_ref, project_id)
    tasks = control_runtime._affected_tasks(task_manager, root)
    restart_opts = restart_controls._effective_restart_options(root, opts)
    if restart_opts is not None:
        restart_controls._validate_restart_options(restart_opts)
    if opts is not None and restart_opts is not None:
        restart_controls._persist_restart_artifacts(task_manager, root, restart_opts)
        restart_controls._apply_restart_task_controls(
            task_manager,
            root,
            tasks,
            restart_opts,
            allow_automation=not no_resume,
        )
    dispatch_failures_reset = restart_controls._reset_restart_dispatch_failures(
        task_manager,
        tasks,
    )
    escalations_cleared = restart_controls._clear_restartable_escalations(
        task_manager,
        tasks,
    )
    restart_stage_resets = restart_controls._reset_restart_stage_manifests(
        db,
        root,
        tasks,
        restart_opts,
    )
    restart_manifest = (
        restart_controls._root_manifest_payload(task_manager, root.id) if restart_opts else []
    )
    if no_resume:
        clean_result.action = "restart"
        clean_result.automation_updated = stop_result.automation_updated
        clean_result.mutexes_cleared = stop_result.mutexes_cleared + clean_result.mutexes_cleared
        clean_result.claims_released = stop_result.claims_released + clean_result.claims_released
        clean_result.parked_runs_released = stop_result.parked_runs_released
        clean_result.stages_reset += restart_stage_resets
        clean_result.escalations_cleared = escalations_cleared
        clean_result.dispatch_failures_reset = dispatch_failures_reset
        clean_result.dispatcher_tick = None
        clean_result.manifest = restart_manifest
        build_results._record_target_history(db, clean_result, input_ref=input_ref)
        return clean_result
    resume_result = await build_resume_target(
        input_ref,
        db=db,
        project_id=project_id,
        services=services,
    )
    clean_result.action = "restart"
    clean_result.automation_updated = resume_result.automation_updated
    clean_result.mutexes_cleared = resume_result.mutexes_cleared
    clean_result.claims_released = resume_result.claims_released
    clean_result.parked_runs_released = stop_result.parked_runs_released
    clean_result.stages_reset += restart_stage_resets
    clean_result.escalations_cleared = escalations_cleared
    clean_result.dispatch_failures_reset = dispatch_failures_reset
    clean_result.dispatcher_tick = resume_result.dispatcher_tick
    clean_result.manifest = restart_manifest
    build_results._record_target_history(db, clean_result, input_ref=input_ref)
    return clean_result
