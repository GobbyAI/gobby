"""Task-scoped build lifecycle controls."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from gobby.agents.kill import kill_agent
from gobby.build.branch_cleanup import delete_orphan_build_branches
from gobby.build.control_artifacts import (
    BuildArtifactSummary,
    collect_clean_artifacts,
    defer_active_agent_artifacts,
    defer_dirty_descendant_worktree_artifacts,
    delete_artifacts,
    get_project_path,
)
from gobby.build.dispatch_tick import (
    DispatcherTickSummary,
)
from gobby.build.dispatch_tick import (
    kick_dispatcher_tick as _kick_dispatcher_tick,
)
from gobby.build.options import BuildOptions
from gobby.build.stage_manifest import (
    InputKind,
    _validate_skip_stages,
    resolve_stage_manifest_specs,
)
from gobby.build.validation import _validate_no_merge, _validate_planning_seed, _validate_retry_caps
from gobby.config.build import Isolation
from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES, AgentRun, LocalAgentRunManager
from gobby.storage.build_history import best_effort_record_event, best_effort_record_run
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, StageManifestSpec, StageState, Task
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._stage_manifest import derive_child_manifest_specs
from gobby.storage.tasks._transitions import reset_current_non_ready_stage

logger = logging.getLogger(__name__)

BuildTargetAction = Literal["stop", "resume", "clean", "restart"]
ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS = 30


@dataclass(frozen=True)
class BuildTaskSummary:
    """Task touched by a task-scoped build control."""

    task_id: str
    ref: str
    title: str
    task_type: str


@dataclass(frozen=True)
class BuildAgentSummary:
    """Active agent affected by a task-scoped build control."""

    run_id: str
    task_id: str | None
    status: str
    child_session_id: str | None
    worktree_id: str | None
    clone_id: str | None


@dataclass
class BuildTargetControlResult:
    """Result returned by task-scoped build lifecycle controls."""

    action: BuildTargetAction
    project_id: str
    root_task_id: str
    affected_tasks: list[BuildTaskSummary]
    agents: list[BuildAgentSummary] = field(default_factory=list)
    artifacts: list[BuildArtifactSummary] = field(default_factory=list)
    dry_run: bool = False
    force: bool = False
    automation_updated: int = 0
    mutexes_cleared: int = 0
    claims_released: int = 0
    stages_reset: int = 0
    branches_deleted: int = 0
    escalations_cleared: int = 0
    dispatch_failures_reset: int = 0
    dispatcher_tick: DispatcherTickSummary | None = None
    manifest: list[dict[str, Any]] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


async def build_stop_target(
    input_ref: str,
    *,
    db: HubDatabase,
    project_id: str,
    services: object | None = None,
) -> BuildTargetControlResult:
    """Stop automation for a single task or epic subtree."""
    task_manager = LocalTaskManager(db)
    root = _resolve_task_ref(task_manager, input_ref, project_id)
    tasks = _affected_tasks(task_manager, root)
    task_ids = [task.id for task in tasks]
    agents = _active_agents(db, task_ids)

    updated = 0
    for task in tasks:
        task_manager.update_task(task.id, allow_automation=False, unattended=False)
        updated += 1

    await _cancel_active_agents(db, agents, services=services)
    mutexes_cleared = _clear_dispatch_mutexes(db, task_ids)
    claims_released = _release_stale_agent_claims(task_manager, db, tasks)
    stages_reset = _reset_current_stages(db, tasks, reason="build_stop")

    result = BuildTargetControlResult(
        action="stop",
        project_id=project_id,
        root_task_id=root.id,
        affected_tasks=_task_summaries(tasks),
        agents=_agent_summaries(agents),
        automation_updated=updated,
        mutexes_cleared=mutexes_cleared,
        claims_released=claims_released,
        stages_reset=stages_reset,
    )
    _record_target_history(db, result, input_ref=input_ref)
    return result


async def build_resume_target(
    input_ref: str,
    *,
    db: HubDatabase,
    project_id: str,
    services: object | None = None,
) -> BuildTargetControlResult:
    """Resume automation for a single task or epic subtree."""
    task_manager = LocalTaskManager(db)
    root = _resolve_task_ref(task_manager, input_ref, project_id)
    tasks = _affected_tasks(task_manager, root)
    task_ids = [task.id for task in tasks]

    updated = 0
    for task in tasks:
        task_manager.update_task(task.id, allow_automation=True)
        updated += 1

    mutexes_cleared = _clear_stale_dispatch_mutexes(db, task_ids)
    claims_released = _release_stale_agent_claims(task_manager, db, tasks)
    tick = await _kick_dispatcher_tick(db, project_id, services=services)

    result = BuildTargetControlResult(
        action="resume",
        project_id=project_id,
        root_task_id=root.id,
        affected_tasks=_task_summaries(tasks),
        automation_updated=updated,
        mutexes_cleared=mutexes_cleared,
        claims_released=claims_released,
        dispatcher_tick=tick,
    )
    _record_target_history(db, result, input_ref=input_ref)
    return result


async def build_clean_target(
    input_ref: str,
    *,
    db: HubDatabase,
    project_id: str,
    dry_run: bool = False,
    force: bool = False,
    yes: bool = False,
    services: object | None = None,
) -> BuildTargetControlResult:
    """Delete failed build artifacts for a single task or epic subtree."""
    if not dry_run and not yes:
        raise ValueError("clean is destructive; pass yes=True to confirm")

    task_manager = LocalTaskManager(db)
    root = _resolve_task_ref(task_manager, input_ref, project_id)
    tasks = _affected_tasks(task_manager, root)
    task_ids = [task.id for task in tasks]
    agents = _active_agents(db, task_ids)
    artifacts = collect_clean_artifacts(db, project_id, tasks)
    blocked = _clean_blockers(tasks, agents, force=force)

    if dry_run:
        result = BuildTargetControlResult(
            action="clean",
            project_id=project_id,
            root_task_id=root.id,
            affected_tasks=_task_summaries(tasks),
            agents=_agent_summaries(agents),
            artifacts=artifacts,
            dry_run=True,
            force=force,
            blocked_reasons=blocked,
        )
        _record_target_history(db, result, input_ref=input_ref)
        return result

    if blocked:
        raise ValueError("; ".join(blocked))

    if force and agents:
        await _cancel_active_agents(db, agents, services=services)

    delete_artifacts(db, project_id, artifacts, force=force)
    delete_errors = [artifact.error for artifact in artifacts if artifact.error]
    branches_deleted, branch_errors = delete_orphan_build_branches(
        db,
        project_id,
        tasks,
    )
    cleanup_errors = [*delete_errors, *branch_errors]
    if cleanup_errors:
        raise ValueError("; ".join(cleanup_errors))

    mutexes_cleared = _clear_dispatch_mutexes(db, task_ids)
    claims_released = _release_stale_agent_claims(task_manager, db, tasks)
    stages_reset = _reset_current_stages(db, tasks, reason="build_clean")

    result = BuildTargetControlResult(
        action="clean",
        project_id=project_id,
        root_task_id=root.id,
        affected_tasks=_task_summaries(tasks),
        agents=_agent_summaries(agents),
        artifacts=artifacts,
        force=force,
        mutexes_cleared=mutexes_cleared,
        claims_released=claims_released,
        stages_reset=stages_reset,
        branches_deleted=branches_deleted,
    )
    _record_target_history(db, result, input_ref=input_ref)
    return result


def cleanup_successful_merge_artifacts(
    db: HubDatabase,
    task_id: str,
    *,
    project_id: str | None = None,
) -> list[BuildArtifactSummary]:
    """Best-effort cleanup for build artifacts after a merge stage succeeds."""
    task_manager = LocalTaskManager(db)
    root = task_manager.get_task(task_id, project_id=project_id)
    cleanup_project_id = project_id or root.project_id
    tasks = _affected_tasks(task_manager, root)
    artifacts = collect_clean_artifacts(db, cleanup_project_id, tasks)
    if not artifacts:
        return []

    active_agents = _active_agents(db, [task.id for task in tasks])
    artifacts_to_delete = defer_active_agent_artifacts(artifacts, active_agents)
    artifacts_to_delete = defer_dirty_descendant_worktree_artifacts(
        artifacts_to_delete,
        root_task_id=root.id,
        project_path=get_project_path(db, cleanup_project_id),
    )

    delete_artifacts(
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
) -> BuildTargetControlResult:
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
        _record_target_history(db, preview, input_ref=input_ref)
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
    root = _resolve_task_ref(task_manager, input_ref, project_id)
    tasks = _affected_tasks(task_manager, root)
    restart_opts = _effective_restart_options(root, opts)
    if restart_opts is not None:
        _validate_restart_options(restart_opts)
        _persist_restart_artifacts(task_manager, root, restart_opts)
        _apply_restart_task_controls(
            task_manager,
            root,
            tasks,
            restart_opts,
            allow_automation=not no_resume,
        )
    dispatch_failures_reset = _reset_restart_dispatch_failures(task_manager, tasks)
    escalations_cleared = _clear_restartable_escalations(task_manager, tasks)
    restart_stage_resets = _reset_restart_stage_manifests(db, root, tasks, restart_opts)
    restart_manifest = _root_manifest_payload(task_manager, root.id) if restart_opts else []
    if no_resume:
        clean_result.action = "restart"
        clean_result.automation_updated = stop_result.automation_updated
        clean_result.mutexes_cleared = stop_result.mutexes_cleared + clean_result.mutexes_cleared
        clean_result.claims_released = stop_result.claims_released + clean_result.claims_released
        clean_result.stages_reset += restart_stage_resets
        clean_result.escalations_cleared = escalations_cleared
        clean_result.dispatch_failures_reset = dispatch_failures_reset
        clean_result.dispatcher_tick = None
        clean_result.manifest = restart_manifest
        _record_target_history(db, clean_result, input_ref=input_ref)
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
    clean_result.stages_reset += restart_stage_resets
    clean_result.escalations_cleared = escalations_cleared
    clean_result.dispatch_failures_reset = dispatch_failures_reset
    clean_result.dispatcher_tick = resume_result.dispatcher_tick
    clean_result.manifest = restart_manifest
    _record_target_history(db, clean_result, input_ref=input_ref)
    return clean_result


def _resolve_task_ref(
    task_manager: LocalTaskManager,
    input_ref: str,
    project_id: str,
) -> Task:
    try:
        resolved_id = task_manager.resolve_task_reference(input_ref, project_id)
        return task_manager.get_task(resolved_id, project_id=project_id)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"task ref not found: {input_ref}") from exc


def _affected_tasks(task_manager: LocalTaskManager, root: Task) -> list[Task]:
    if root.task_type != "epic":
        return [root]

    rows = task_manager.db.fetchall(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id
            FROM tasks
            WHERE id = %s
            UNION ALL
            SELECT child.id
            FROM tasks child
            JOIN subtree parent ON child.parent_task_id = parent.id
        )
        SELECT id
        FROM subtree
        """,
        (root.id,),
    )
    return [task_manager.get_task(row["id"]) for row in rows]


def _task_summaries(tasks: list[Task]) -> list[BuildTaskSummary]:
    return [
        BuildTaskSummary(
            task_id=task.id,
            ref=f"#{task.seq_num}" if task.seq_num else task.id,
            title=task.title,
            task_type=task.task_type,
        )
        for task in tasks
    ]


def _active_agents(db: HubDatabase, task_ids: list[str]) -> list[AgentRun]:
    return LocalAgentRunManager(db).list_active(task_ids=task_ids, limit=1000)


def _agent_summaries(agents: list[AgentRun]) -> list[BuildAgentSummary]:
    return [
        BuildAgentSummary(
            run_id=run.id,
            task_id=run.task_id,
            status=run.status,
            child_session_id=run.child_session_id,
            worktree_id=run.worktree_id,
            clone_id=run.clone_id,
        )
        for run in agents
    ]


async def _cancel_active_agents(
    db: HubDatabase,
    agents: list[AgentRun],
    *,
    services: object | None,
) -> None:
    lifecycle_monitor = getattr(services, "agent_lifecycle_monitor", None)
    run_manager = LocalAgentRunManager(db)

    for run in agents:
        try:
            result = await kill_agent(
                run,
                db,
                signal_name="TERM",
                timeout=5.0,
                close_terminal=True,
            )
            if not result.get("success"):
                logger.info("agent_kill_noop", extra={"run_id": run.id, "result": result})
        except Exception as exc:
            logger.warning("Failed to kill active build agent %s: %s", run.id, exc)

        if lifecycle_monitor is not None:
            transitioned = await lifecycle_monitor.terminalize_cancelled_run(
                run.id,
                terminal_reason="user_cancelled",
            )
        else:
            transitioned = run_manager.cancel(run.id, terminal_reason="user_cancelled") is not None
        if not transitioned:
            logger.debug("Agent %s was already terminal while stopping build", run.id)


def _clear_stale_dispatch_mutexes(
    db: HubDatabase,
    task_ids: list[str],
    *,
    now: datetime | None = None,
) -> int:
    mutexes = TaskDispatchMutexManager(db)
    resolved_now = now or datetime.now(UTC)
    cleared = 0
    active_run_ids = {run.id for run in LocalAgentRunManager(db).list_active(limit=1000)}
    for task_id in task_ids:
        mutex = mutexes.get_mutex(task_id)
        if mutex is None:
            continue
        if mutex.run_id:
            if mutex.run_id not in active_run_ids and mutexes.force_release(task_id):
                cleared += 1
            continue
        if _is_orphan_no_run_dispatch_mutex(mutex, now=resolved_now):
            if mutexes.force_release(task_id):
                cleared += 1
    return cleared


def _is_orphan_no_run_dispatch_mutex(mutex: Any, *, now: datetime) -> bool:
    if getattr(mutex, "lease_holder", None) != "dispatcher":
        return False
    if getattr(mutex, "run_id", None):
        return False

    lease_until = _parse_mutex_timestamp(getattr(mutex, "lease_until", None))
    if lease_until is not None:
        return lease_until < now

    updated_at = _parse_mutex_timestamp(getattr(mutex, "updated_at", None))
    if updated_at is None:
        return False
    return now - updated_at >= timedelta(seconds=ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS)


def _parse_mutex_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _clear_dispatch_mutexes(db: HubDatabase, task_ids: list[str]) -> int:
    mutexes = TaskDispatchMutexManager(db)
    cleared = int(mutexes.sweep_expired())
    for task_id in task_ids:
        if mutexes.force_release(task_id):
            cleared += 1
    return cleared


def _release_stale_agent_claims(
    task_manager: LocalTaskManager,
    db: HubDatabase,
    tasks: list[Task],
) -> int:
    active_session_ids = {
        session_id
        for run in LocalAgentRunManager(db).list_active(limit=1000)
        for session_id in (run.child_session_id, run.claimed_session_id, run.parent_session_id)
        if session_id
    }
    released = 0
    for task in tasks:
        claim = task.claimed_by_session_id
        if not claim or claim in active_session_ids:
            continue
        if not _has_terminal_agent_claim(db, task.id, claim):
            continue
        task_manager.release_task_claim(task.id)
        released += 1
    return released


def _has_terminal_agent_claim(db: HubDatabase, task_id: str, session_id: str) -> bool:
    rows = db.fetchall(
        """
        SELECT status
        FROM agent_runs
        WHERE task_id = %s
          AND (
            child_session_id = %s
            OR claimed_session_id = %s
            OR parent_session_id = %s
          )
        """,
        (task_id, session_id, session_id, session_id),
    )
    return any(row["status"] not in ACTIVE_AGENT_RUN_STATUSES for row in rows)


def _reset_current_stages(db: HubDatabase, tasks: list[Task], *, reason: str) -> int:
    reset = 0
    for task in tasks:
        if reset_current_non_ready_stage(db, task.id, reason=reason, by_actor="build"):
            reset += 1
    return reset


def _clear_restartable_escalations(task_manager: LocalTaskManager, tasks: list[Task]) -> int:
    cleared = 0
    for task in tasks:
        if task.closed_at is not None or not task.is_escalated:
            continue
        if not _is_build_owned_escalation(task.escalation_reason):
            continue
        task_manager.release_task_claim(
            task.id,
            escalated_at=None,
            escalation_reason=None,
            dispatch_failure_count=0,
            validation_fail_count=0,
        )
        cleared += 1
    return cleared


def _reset_restart_dispatch_failures(task_manager: LocalTaskManager, tasks: list[Task]) -> int:
    reset = 0
    for task in tasks:
        if task.closed_at is not None or int(task.dispatch_failure_count or 0) <= 0:
            continue
        task_manager.update_task(task.id, dispatch_failure_count=0)
        reset += 1
    return reset


def _is_build_owned_escalation(reason: str | None) -> bool:
    if not reason:
        return False
    if reason.endswith(
        (
            "_max_work_attempts",
            "_max_review_rounds",
            "_work_failed:max",
            "_review_failed:max",
        )
    ):
        return True
    return reason.startswith(
        (
            "dispatch_spawn_max_attempts:",
            "stage_pipeline_dispatch:",
            "isolation_missing_target_branch",
        )
    )


def _effective_restart_options(root: Task, opts: BuildOptions | None) -> BuildOptions | None:
    if opts is None:
        return None
    if opts.isolation_explicit:
        return opts
    return replace(opts, isolation=_task_isolation(root))


def _task_isolation(task: Task) -> Isolation:
    isolation = getattr(task.isolation, "value", task.isolation)
    if isolation in {"none", "worktree", "clone"}:
        return cast(Isolation, isolation)
    return "worktree"


def _validate_restart_options(opts: BuildOptions) -> None:
    _validate_no_merge(opts)
    _validate_retry_caps(opts)
    _validate_planning_seed(opts)


def _persist_restart_artifacts(
    task_manager: LocalTaskManager,
    root: Task,
    opts: BuildOptions,
) -> None:
    if opts.target_branch is None:
        return
    task_manager.artifacts.set_artifact(root.id, "target_branch", opts.target_branch)


def _apply_restart_task_controls(
    task_manager: LocalTaskManager,
    root: Task,
    tasks: list[Task],
    opts: BuildOptions,
    *,
    allow_automation: bool,
) -> None:
    for task in tasks:
        if task.closed_at is not None:
            continue
        if task.id == root.id and opts.assigned_agent is not None:
            task_manager.update_task(
                task.id,
                allow_automation=allow_automation,
                unattended=opts.unattended,
                isolation=opts.isolation,
                assigned_agent=opts.assigned_agent,
            )
        else:
            task_manager.update_task(
                task.id,
                allow_automation=allow_automation,
                unattended=opts.unattended,
                isolation=opts.isolation,
            )


def _reset_restart_stage_manifests(
    db: HubDatabase,
    root: Task,
    tasks: list[Task],
    opts: BuildOptions | None,
) -> int:
    if opts is not None:
        return _reset_restart_stage_manifests_from_options(db, root, tasks, opts)
    return _reset_restart_stage_manifests_legacy(db, tasks)


def _reset_restart_stage_manifests_legacy(db: HubDatabase, tasks: list[Task]) -> int:
    task_manager = LocalTaskManager(db)
    reset = 0
    for task in tasks:
        if task.closed_at is not None:
            continue
        rows = task_manager.stage_states.list_for_task(task.id)
        if not rows:
            continue
        specs = _restart_stage_specs(db, task, rows)
        db.execute("DELETE FROM task_stage_states WHERE task_id = %s", (task.id,))
        task_manager.stage_states.initialize_manifest(task.id, specs, by_session_id=None)
        reset += 1
    return reset


def _reset_restart_stage_manifests_from_options(
    db: HubDatabase,
    root: Task,
    tasks: list[Task],
    opts: BuildOptions,
) -> int:
    task_manager = LocalTaskManager(db)
    skip_stages = _validate_skip_stages(opts.skip_stages)
    input_kind = _restart_root_input_kind(task_manager, root)
    root_specs = resolve_stage_manifest_specs(task_manager, root, input_kind, opts, skip_stages)
    reset = 0
    for task in tasks:
        if task.closed_at is not None:
            continue
        specs = (
            root_specs
            if task.id == root.id
            else derive_child_manifest_specs(
                root_specs,
                include_holistic_qa=task.task_type == "epic",
                include_merge_stage=opts.isolation in {"worktree", "clone"} and not opts.no_merge,
            )
        )
        if not specs:
            continue
        db.execute("DELETE FROM task_stage_states WHERE task_id = %s", (task.id,))
        task_manager.stage_states.initialize_manifest(task.id, specs, by_session_id=None)
        reset += 1
    if input_kind == "plan_file":
        _seed_restart_plan_file_stage_state(task_manager, root.id, opts)
    return reset


def _root_manifest_payload(task_manager: LocalTaskManager, task_id: str) -> list[dict[str, Any]]:
    return [
        {
            "stage_name": row.stage_name,
            "position": row.position,
            "max_work_attempts": row.max_work_attempts,
            "max_review_rounds": row.max_review_rounds,
        }
        for row in task_manager.stage_states.list_for_task(task_id)
    ]


def _restart_root_input_kind(task_manager: LocalTaskManager, root: Task) -> InputKind:
    artifacts = task_manager.artifacts.get_artifacts(root.id)
    if artifacts.plan_file_path:
        return "plan_file"
    if root.task_type != "epic":
        return "leaf"
    if _has_children(task_manager.db, root.id):
        return "expanded_epic"
    return "epic"


def _seed_restart_plan_file_stage_state(
    task_manager: LocalTaskManager,
    task_id: str,
    opts: BuildOptions,
) -> None:
    if opts.planning_seed_state != "needs_review":
        return
    if not task_manager.stage_states.get(task_id, "planning"):
        raise ValueError("planning_seed_state=needs_review requires a planning stage")
    now = datetime.now(UTC).isoformat()
    with task_manager.db.transaction() as conn:
        conn.execute(
            """
            UPDATE task_stage_states
               SET state = 'needs_review',
                   review_round_count = %s,
                   entered_at = COALESCE(entered_at, %s),
                   updated_at = %s,
                   notes = %s
             WHERE task_id = %s
               AND stage_name = 'planning'
            """,
            (
                opts.completed_plan_review_rounds,
                now,
                now,
                "Seeded plan review state from build restart input.",
                task_id,
            ),
        )


def _restart_stage_specs(
    db: HubDatabase,
    task: Task,
    rows: list[StageState],
) -> list[StageManifestSpec]:
    by_name = {row.stage_name: row for row in rows}
    if _task_uses_isolated_workspace(task):
        if task.task_type == "epic" and _has_children(db, task.id):
            stage_names = ["development", "holistic_qa", "merge"]
        else:
            stage_names = [_primary_stage_for_restart(task), "merge"]
    else:
        stage_names = [row.stage_name for row in sorted(rows, key=lambda item: item.position)]

    specs: list[StageManifestSpec] = []
    for position, stage_name in enumerate(stage_names):
        source = by_name.get(stage_name)
        specs.append(
            StageManifestSpec(
                stage_name=stage_name,
                position=position,
                max_work_attempts=getattr(source, "max_work_attempts", None),
                max_review_rounds=getattr(source, "max_review_rounds", None),
            )
        )
    return specs


def _task_uses_isolated_workspace(task: Task) -> bool:
    isolation = getattr(task.isolation, "value", task.isolation)
    return isolation in {"worktree", "clone"}


def _has_children(db: HubDatabase, task_id: str) -> bool:
    return bool(db.fetchone("SELECT 1 FROM tasks WHERE parent_task_id = %s LIMIT 1", (task_id,)))


def _primary_stage_for_restart(task: Task) -> str:
    return {
        "code": "development",
        "config": "development",
        "docs": "development",
        "refactor": "development",
        "test": "development",
        "research": "research",
        "planning": "planning",
    }.get(task.category or "", "development")


def _clean_blockers(
    tasks: list[Task],
    agents: list[AgentRun],
    *,
    force: bool,
) -> list[str]:
    blockers: list[str] = []
    if not force:
        active_refs = [f"#{task.seq_num}" for task in tasks if task.allow_automation]
        if active_refs:
            blockers.append(
                "automation must be stopped before clean; active tasks: " + ", ".join(active_refs)
            )
        if agents:
            blockers.append(
                "live agents must be stopped before clean; active runs: "
                + ", ".join(run.id for run in agents)
            )
    return blockers


def _record_target_history(
    db: HubDatabase,
    result: BuildTargetControlResult,
    *,
    input_ref: str,
) -> None:
    summary = result.to_dict()
    run = best_effort_record_run(
        db,
        project_id=result.project_id,
        root_task_id=result.root_task_id,
        input_ref=input_ref,
        action=result.action,
        status="completed",
        actor="build",
        summary=summary,
    )
    best_effort_record_event(
        db,
        run_id=run.id if run is not None else None,
        project_id=result.project_id,
        root_task_id=result.root_task_id,
        event_type="task_build_control",
        action=result.action,
        message=f"gobby build {result.action}",
        payload=summary,
    )


__all__ = [
    "BuildAgentSummary",
    "BuildArtifactSummary",
    "BuildTargetControlResult",
    "BuildTaskSummary",
    "build_clean_target",
    "build_restart_target",
    "build_resume_target",
    "build_stop_target",
]
