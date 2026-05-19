"""Task-scoped build lifecycle controls."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from gobby.agents.kill import kill_agent
from gobby.build.branch_cleanup import (
    default_task_branch_name,
    delete_orphan_build_branches,
)
from gobby.build.dispatch_tick import (
    DispatcherTickSummary,
)
from gobby.build.dispatch_tick import (
    kick_dispatcher_tick as _kick_dispatcher_tick,
)
from gobby.clones.git import CloneGitManager
from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES, AgentRun, LocalAgentRunManager
from gobby.storage.clones import LocalCloneManager
from gobby.storage.database import DatabaseProtocol
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, StageManifestSpec, StageState, Task
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._transitions import reset_current_non_ready_stage
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)

BuildTargetAction = Literal["stop", "resume", "clean", "restart"]
ArtifactFamily = Literal["worktree", "clone"]


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
class BuildArtifactSummary:
    """Build artifact considered or removed by a clean operation."""

    family: ArtifactFamily
    task_id: str | None
    path: str
    artifact_id: str | None = None
    source: str = "tracked"
    orphan: bool = False
    exists: bool = False
    deleted: bool = False
    deferred: bool = False
    error: str | None = None


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
    blocked_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


async def build_stop_target(
    input_ref: str,
    *,
    db: DatabaseProtocol,
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

    return BuildTargetControlResult(
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


async def build_resume_target(
    input_ref: str,
    *,
    db: DatabaseProtocol,
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

    return BuildTargetControlResult(
        action="resume",
        project_id=project_id,
        root_task_id=root.id,
        affected_tasks=_task_summaries(tasks),
        automation_updated=updated,
        mutexes_cleared=mutexes_cleared,
        claims_released=claims_released,
        dispatcher_tick=tick,
    )


async def build_clean_target(
    input_ref: str,
    *,
    db: DatabaseProtocol,
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
    artifacts = _collect_clean_artifacts(db, project_id, tasks)
    blocked = _clean_blockers(tasks, agents, force=force)

    if dry_run:
        return BuildTargetControlResult(
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

    if blocked:
        raise ValueError("; ".join(blocked))

    if force and agents:
        await _cancel_active_agents(db, agents, services=services)

    _delete_artifacts(db, project_id, artifacts, force=force)
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

    return BuildTargetControlResult(
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


def cleanup_successful_merge_artifacts(
    db: DatabaseProtocol,
    task_id: str,
    *,
    project_id: str | None = None,
) -> list[BuildArtifactSummary]:
    """Best-effort cleanup for build artifacts after a merge stage succeeds."""
    task_manager = LocalTaskManager(db)
    root = task_manager.get_task(task_id, project_id=project_id)
    cleanup_project_id = project_id or root.project_id
    tasks = _affected_tasks(task_manager, root)
    artifacts = _collect_clean_artifacts(db, cleanup_project_id, tasks)
    if not artifacts:
        return []

    active_agents = _active_agents(db, [task.id for task in tasks])
    artifacts_to_delete = _defer_active_agent_artifacts(artifacts, active_agents)

    _delete_artifacts(db, cleanup_project_id, artifacts_to_delete, force=False)
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
    db: DatabaseProtocol,
    project_id: str,
    dry_run: bool = False,
    force: bool = False,
    yes: bool = False,
    no_resume: bool = False,
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
    dispatch_failures_reset = _reset_restart_dispatch_failures(task_manager, tasks)
    escalations_cleared = _clear_restartable_escalations(task_manager, tasks)
    restart_stage_resets = _reset_restart_stage_manifests(db, tasks)
    if no_resume:
        clean_result.action = "restart"
        clean_result.automation_updated = stop_result.automation_updated
        clean_result.mutexes_cleared = stop_result.mutexes_cleared + clean_result.mutexes_cleared
        clean_result.claims_released = stop_result.claims_released + clean_result.claims_released
        clean_result.stages_reset += restart_stage_resets
        clean_result.escalations_cleared = escalations_cleared
        clean_result.dispatch_failures_reset = dispatch_failures_reset
        clean_result.dispatcher_tick = None
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
            WHERE id = ?
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


def _active_agents(db: DatabaseProtocol, task_ids: list[str]) -> list[AgentRun]:
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


def _defer_active_agent_artifacts(
    artifacts: list[BuildArtifactSummary],
    agents: list[AgentRun],
) -> list[BuildArtifactSummary]:
    active_worktree_ids = {run.worktree_id for run in agents if run.worktree_id}
    active_clone_ids = {run.clone_id for run in agents if run.clone_id}
    artifacts_to_delete: list[BuildArtifactSummary] = []

    for artifact in artifacts:
        if artifact.family == "worktree" and artifact.artifact_id in active_worktree_ids:
            artifact.deferred = True
            continue
        if artifact.family == "clone" and artifact.artifact_id in active_clone_ids:
            artifact.deferred = True
            continue
        artifacts_to_delete.append(artifact)

    return artifacts_to_delete


async def _cancel_active_agents(
    db: DatabaseProtocol,
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


def _clear_stale_dispatch_mutexes(db: DatabaseProtocol, task_ids: list[str]) -> int:
    mutexes = TaskDispatchMutexManager(db)
    cleared = mutexes.sweep_expired()
    active_run_ids = {run.id for run in LocalAgentRunManager(db).list_active(limit=1000)}
    for task_id in task_ids:
        mutex = mutexes.get_mutex(task_id)
        if mutex is not None and mutex.run_id and mutex.run_id not in active_run_ids:
            if mutexes.force_release(task_id):
                cleared += 1
    return cleared


def _clear_dispatch_mutexes(db: DatabaseProtocol, task_ids: list[str]) -> int:
    mutexes = TaskDispatchMutexManager(db)
    cleared = mutexes.sweep_expired()
    for task_id in task_ids:
        if mutexes.force_release(task_id):
            cleared += 1
    return cleared


def _release_stale_agent_claims(
    task_manager: LocalTaskManager,
    db: DatabaseProtocol,
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


def _has_terminal_agent_claim(db: DatabaseProtocol, task_id: str, session_id: str) -> bool:
    rows = db.fetchall(
        """
        SELECT status
        FROM agent_runs
        WHERE task_id = ?
          AND (
            child_session_id = ?
            OR claimed_session_id = ?
            OR parent_session_id = ?
          )
        """,
        (task_id, session_id, session_id, session_id),
    )
    return any(row["status"] not in ACTIVE_AGENT_RUN_STATUSES for row in rows)


def _reset_current_stages(db: DatabaseProtocol, tasks: list[Task], *, reason: str) -> int:
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


def _reset_restart_stage_manifests(db: DatabaseProtocol, tasks: list[Task]) -> int:
    task_manager = LocalTaskManager(db)
    reset = 0
    for task in tasks:
        if task.closed_at is not None:
            continue
        rows = task_manager.stage_states.list_for_task(task.id)
        if not rows:
            continue
        specs = _restart_stage_specs(db, task, rows)
        db.execute("DELETE FROM task_stage_states WHERE task_id = ?", (task.id,))
        task_manager.stage_states.initialize_manifest(task.id, specs, by_session_id=None)
        reset += 1
    return reset


def _restart_stage_specs(
    db: DatabaseProtocol,
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


def _has_children(db: DatabaseProtocol, task_id: str) -> bool:
    return bool(db.fetchone("SELECT 1 FROM tasks WHERE parent_task_id = ? LIMIT 1", (task_id,)))


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


def _collect_clean_artifacts(
    db: DatabaseProtocol,
    project_id: str,
    tasks: list[Task],
) -> list[BuildArtifactSummary]:
    worktrees = LocalWorktreeManager(db)
    clones = LocalCloneManager(db)
    summaries: list[BuildArtifactSummary] = []
    seen: set[tuple[str, str]] = set()

    for task in tasks:
        artifacts = LocalTaskManager(db).artifacts.get_artifacts(task.id)
        _append_artifact(
            summaries,
            seen,
            family="worktree",
            task_id=task.id,
            path=artifacts.worktree_path,
            artifact_id=artifacts.worktree_id,
            source="task_artifacts",
        )
        _append_artifact(
            summaries,
            seen,
            family="clone",
            task_id=task.id,
            path=artifacts.clone_path,
            artifact_id=artifacts.clone_id,
            source="task_artifacts",
        )
        if artifacts.integration_workspace_id:
            integration_worktree = worktrees.get(artifacts.integration_workspace_id)
            if integration_worktree is not None:
                _append_artifact(
                    summaries,
                    seen,
                    family="worktree",
                    task_id=task.id,
                    path=integration_worktree.worktree_path,
                    artifact_id=integration_worktree.id,
                    source="task_artifacts_integration",
                )
        if artifacts.integration_clone_id:
            integration_clone = clones.get(artifacts.integration_clone_id)
            if integration_clone is not None:
                _append_artifact(
                    summaries,
                    seen,
                    family="clone",
                    task_id=task.id,
                    path=integration_clone.clone_path,
                    artifact_id=integration_clone.id,
                    source="task_artifacts_integration",
                )

        worktree = worktrees.get_by_task(task.id)
        if worktree is not None:
            _append_artifact(
                summaries,
                seen,
                family="worktree",
                task_id=task.id,
                path=worktree.worktree_path,
                artifact_id=worktree.id,
                source="worktrees_integration"
                if worktree.workspace_role == "integration"
                else "worktrees",
            )
        clone = clones.get_by_task(task.id)
        if clone is not None:
            _append_artifact(
                summaries,
                seen,
                family="clone",
                task_id=task.id,
                path=clone.clone_path,
                artifact_id=clone.id,
                source="clones_integration" if clone.workspace_role == "integration" else "clones",
            )

    summaries.extend(_detect_orphan_artifacts(db, project_id, tasks, seen))
    return summaries


def _append_artifact(
    summaries: list[BuildArtifactSummary],
    seen: set[tuple[str, str]],
    *,
    family: ArtifactFamily,
    task_id: str | None,
    path: str | None,
    artifact_id: str | None,
    source: str,
) -> None:
    if not path:
        return
    expanded_path = Path(path).expanduser()
    key = (family, str(expanded_path))
    if key in seen:
        return
    seen.add(key)
    summaries.append(
        BuildArtifactSummary(
            family=family,
            task_id=task_id,
            path=str(expanded_path),
            artifact_id=artifact_id,
            source=source,
            exists=expanded_path.exists(),
        )
    )


def _detect_orphan_artifacts(
    db: DatabaseProtocol,
    project_id: str,
    tasks: list[Task],
    seen: set[tuple[str, str]],
) -> list[BuildArtifactSummary]:
    project_path = _project_path(db, project_id)
    project_name = project_path.name
    roots: dict[ArtifactFamily, Path] = {
        "worktree": Path.home() / ".gobby" / "worktrees" / project_name,
        "clone": Path.home() / ".gobby" / "clones" / project_name,
    }
    orphan_summaries: list[BuildArtifactSummary] = []

    for task in tasks:
        if not task.seq_num:
            continue
        prefix = f"task-{task.seq_num}-"
        expected = default_task_branch_name(task)
        for family, root in roots.items():
            if not root.exists() or not root.is_dir():
                continue
            for candidate in root.iterdir():
                if candidate.name != expected and not candidate.name.startswith(prefix):
                    continue
                key = (family, str(candidate))
                if key in seen:
                    continue
                seen.add(key)
                orphan_summaries.append(
                    BuildArtifactSummary(
                        family=family,
                        task_id=task.id,
                        path=str(candidate),
                        source="orphan",
                        orphan=True,
                        exists=candidate.exists(),
                    )
                )
    return orphan_summaries


def _delete_artifacts(
    db: DatabaseProtocol,
    project_id: str,
    artifacts: list[BuildArtifactSummary],
    *,
    force: bool,
) -> None:
    project_path = _project_path(db, project_id)
    worktree_git = WorktreeGitManager(project_path)
    clone_git = CloneGitManager(project_path)
    worktrees = LocalWorktreeManager(db)
    clones = LocalCloneManager(db)
    task_manager = LocalTaskManager(db)

    for artifact in artifacts:
        try:
            path = Path(artifact.path)
            if artifact.family == "worktree":
                if path.exists():
                    worktree_result = worktree_git.delete_worktree(path, force=force)
                    if not worktree_result.success:
                        artifact.error = worktree_result.error or worktree_result.message
                        continue
                if artifact.artifact_id:
                    worktrees.delete(artifact.artifact_id)
            else:
                if path.exists():
                    clone_result = clone_git.delete_clone(path, force=force)
                    if not clone_result.success:
                        artifact.error = clone_result.error or clone_result.message
                        continue
                if artifact.artifact_id:
                    clones.delete(artifact.artifact_id)

            if artifact.task_id is not None and artifact.source.endswith("_integration"):
                task_manager.artifacts.set_artifacts_atomic(
                    artifact.task_id,
                    integration_branch=None,
                    integration_workspace_id=None,
                    integration_clone_id=None,
                )
            elif artifact.task_id is not None and not artifact.orphan:
                task_manager.artifacts.clear_isolation_pair(artifact.task_id, artifact.family)
            artifact.exists = False
            artifact.deleted = True
        except Exception as exc:
            artifact.error = str(exc)


def _project_path(db: DatabaseProtocol, project_id: str) -> Path:
    project = LocalProjectManager(db).get(project_id)
    if project is not None and project.repo_path:
        return Path(project.repo_path)
    return Path.cwd()


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
