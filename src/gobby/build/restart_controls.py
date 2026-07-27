"""Restart-state reconstruction for task-scoped build controls."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from gobby.build.options import BuildOptions
from gobby.build.stage_manifest import (
    InputKind,
    _validate_skip_stages,
    resolve_stage_manifest_specs,
)
from gobby.build.validation import _validate_no_merge, _validate_planning_seed, _validate_retry_caps
from gobby.config.build import Isolation
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._stage_manifest import derive_child_manifest_specs


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
        return BuildOptions(
            isolation=_task_isolation(root),
            isolation_explicit=False,
            skip_stages=["pr"],
            skip_stages_explicit=False,
        )
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
    if opts is None:
        return 0
    return _reset_restart_stage_manifests_from_options(db, root, tasks, opts)


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
                include_epic_qa=task.task_type == "epic",
                include_merge_stage=opts.isolation in {"worktree", "clone"} and not opts.no_merge,
            )
        )
        if not specs:
            continue
        rows = task_manager.stage_states.list_for_task(task.id)
        current_names = [row.stage_name for row in rows]
        expected_existing_shape = [
            (row.stage_name, row.position, row.max_work_attempts, row.max_review_rounds)
            for row in rows
        ]
        replaced = task_manager.stage_states.replace_manifest(
            task.id,
            specs,
            expected_existing_shape=expected_existing_shape,
            from_state="manifest:" + ",".join(current_names),
            reason="build_restart",
            by_session_id=None,
            by_actor="build",
        )
        if replaced is None:
            raise RuntimeError(f"stage manifest changed while restarting task #{task.seq_num}")
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
    task_manager.artifacts.set_artifacts_atomic(
        task_id,
        plan_enhancement_rounds=opts.plan_enhancement_rounds,
    )


def _has_children(db: HubDatabase, task_id: str) -> bool:
    return bool(db.fetchone("SELECT 1 FROM tasks WHERE parent_task_id = %s LIMIT 1", (task_id,)))
