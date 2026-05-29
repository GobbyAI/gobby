"""Resume support for existing build lifecycles."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from gobby.build.delivery import record_build_delivery_campaign
from gobby.build.lifecycle_state import current_stage_name, record_build_event
from gobby.build.options import BuildOptions, retry_attempt_cap
from gobby.build.results import BuildResult
from gobby.build.runtime_hooks import RuntimeHooks
from gobby.build.stage_manifest import (
    _canonical_stage_name,
    resolve_stage_manifest_specs,
    specs_payload,
    stage_state_specs,
)
from gobby.build.task_lifecycle import (
    has_existing_expansion_output,
    set_automation_for_task_tree,
)
from gobby.build.validation import _validate_task_ref_isolation_artifacts
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, StageState, Task

_EXPANDED_EPIC_LEGACY_ROOT_STAGES = frozenset(
    {"ideation", "research", "architecture", "prd", "planning", "expansion", "pr"}
)

_STAGE_CAP_UPDATE_ASSIGNMENTS = {
    "max_work_attempts": "max_work_attempts = %s",
    "max_review_rounds": "max_review_rounds = %s",
}

_EPIC_WORKSPACE_REFRESH_STAGES = frozenset({"holistic_qa", "pr", "merge"})


async def resume_existing_lifecycle(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    warnings: list[str],
    db: HubDatabase,
    project_id: str,
    services: object | None,
    target_branch: str | None,
    build_run_id: str | None,
    *,
    runtime: RuntimeHooks,
) -> BuildResult:
    resume_skip_stages = skip_stages
    skip_stages_shape_resume = skip_stages_can_shape_expanded_epic_resume(
        task_manager,
        task,
        skip_stages,
    )
    if skip_stages and not skip_stages_shape_resume:
        if opts.skip_stages_explicit:
            raise ValueError(
                "--skip-stage can only shape a new lifecycle; use build restart or clean first"
            )
        warnings.append("Profile skip_stages ignored because the task already has a manifest")
        resume_skip_stages = []
    resume_opts = opts
    repair_expanded_epic_root_manifest_for_resume(
        task_manager,
        task,
        resume_opts,
        resume_skip_stages,
    )
    apply_stage_caps_to_existing_lifecycle(task_manager, task.id, resume_opts)
    record_build_delivery_campaign(
        db,
        project_id=project_id,
        task_id=task.id,
        opts=resume_opts,
    )
    _validate_task_ref_isolation_artifacts(task_manager, task, resume_opts.isolation)
    task_manager.update_task(
        task.id,
        allow_automation=True,
        unattended=opts.unattended,
        isolation=resume_opts.isolation,
        assigned_agent=(
            opts.assigned_agent if opts.assigned_agent is not None else task.assigned_agent
        ),
    )
    specs = stage_state_specs(task_manager, task.id)
    initial_lifecycle = current_stage_name(task_manager, task.id, specs)
    if task.task_type == "epic":
        artifacts = task_manager.artifacts.get_artifacts(task.id)
        integration_target = target_branch or artifacts.target_branch
        if not resume_opts.dry_run:
            task_manager.cascade_build_state_to_subtree(
                task.id,
                isolation=resume_opts.isolation,
                unattended=opts.unattended,
                allow_automation=True,
                include_merge_stage=resume_opts.isolation in {"worktree", "clone"}
                and not opts.no_merge,
            )
            specs = stage_state_specs(task_manager, task.id)
        initial_lifecycle = current_stage_name(task_manager, task.id, specs)
        if resume_opts.isolation in {"worktree", "clone"}:
            if integration_target is None:
                raise ValueError("target_branch is required for epic integration workspaces")
            if not resume_opts.dry_run and _resume_epic_workspace_refresh_required(
                initial_lifecycle
            ):
                await asyncio.to_thread(
                    runtime.ensure_epic_integration_workspaces,
                    task_manager=task_manager,
                    root_task=task,
                    backend=resume_opts.workspace_backend,
                    target_branch=integration_target,
                    project_id=project_id,
                    services=services,
                )
    elif resume_opts.isolation in {"worktree", "clone"} and not resume_opts.dry_run:
        await asyncio.to_thread(
            runtime.ensure_task_parent_integration_workspace,
            task_manager=task_manager,
            task=task,
            backend=resume_opts.workspace_backend,
            project_id=project_id,
            services=services,
            base_branch_override=target_branch,
        )
    record_build_event(task_manager, task.id, initial_lifecycle)
    runtime.attach_build_run_root(db, build_run_id, task.id)
    tick = await runtime.build_dispatcher_tick(
        db,
        project_id,
        opts,
        dispatcher_enabled=True,
        services=services,
        runtime=runtime,
    )
    if opts.quick and not opts.dry_run:
        set_automation_for_task_tree(
            task_manager,
            task,
            False,
            isolation=resume_opts.isolation,
        )
    return BuildResult(
        task_id=task.id,
        created=False,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=resume_skip_stages if skip_stages_shape_resume else [],
        tick_dispatched=tick.ticks,
        dispatcher_tick=tick,
        manifest=specs_payload(specs),
        warnings=warnings,
        dry_run=opts.dry_run,
    )


def _resume_epic_workspace_refresh_required(stage_name: str | None) -> bool:
    """Existing development-stage epics only need a dispatcher tick."""
    return stage_name in _EPIC_WORKSPACE_REFRESH_STAGES


def skip_stages_can_shape_expanded_epic_resume(
    task_manager: LocalTaskManager,
    task: Task,
    skip_stages: list[str],
) -> bool:
    if not skip_stages:
        return False
    return (
        task.task_type == "epic"
        and set(skip_stages) <= {"pr"}
        and has_existing_expansion_output(task_manager, task)
    )


def repair_expanded_epic_root_manifest_for_resume(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
) -> bool:
    if task.task_type != "epic" or not has_existing_expansion_output(task_manager, task):
        return False

    rows = task_manager.stage_states.list_for_task(task.id)
    if not rows:
        return False

    desired_opts = replace(opts, stage_caps=[])
    desired_specs = resolve_stage_manifest_specs(
        task_manager,
        task,
        "expanded_epic",
        desired_opts,
        skip_stages,
    )
    desired_names = [spec.stage_name for spec in desired_specs]
    current_names = [row.stage_name for row in rows]
    if current_names == desired_names:
        return False

    desired_name_set = set(desired_names)
    if not desired_name_set.issubset(current_names):
        return False
    if any(
        stage_name not in desired_name_set and stage_name not in _EXPANDED_EPIC_LEGACY_ROOT_STAGES
        for stage_name in current_names
    ):
        return False

    desired_rows = [row for row in rows if row.stage_name in desired_name_set]
    if not all(is_pristine_resume_stage(row) for row in desired_rows):
        return False

    task_manager.db.execute("DELETE FROM task_stage_states WHERE task_id = %s", (task.id,))
    task_manager.stage_states.initialize_manifest(
        task.id,
        desired_specs,
        by_session_id=None,
    )
    task_manager.lifecycle_events.record_lifecycle_event(
        task.id,
        from_state="manifest:" + ",".join(current_names),
        to_state="manifest:" + ",".join(desired_names),
        reason="repair_expanded_epic_root_manifest",
        by_actor="build",
    )
    return True


def is_pristine_resume_stage(row: StageState) -> bool:
    return (
        row.state == "ready"
        and row.entered_at is None
        and row.completed_at is None
        and row.work_attempt_count == 0
        and row.review_round_count == 0
        and row.artifact_refs is None
        and row.notes is None
    )


def apply_stage_caps_to_existing_lifecycle(
    task_manager: LocalTaskManager,
    task_id: str,
    opts: BuildOptions,
) -> None:
    if not opts.stage_caps and opts.max_retries is None:
        return
    rows = task_manager.stage_states.list_for_task(task_id)
    stage_names = {row.stage_name for row in rows}
    overrides = {_canonical_stage_name(item.stage_name): item for item in opts.stage_caps}
    retry_cap = retry_attempt_cap(opts)
    for override in opts.stage_caps:
        stage_name = _canonical_stage_name(override.stage_name)
        if stage_name not in stage_names:
            raise ValueError(f"--stage target stage is not in the existing lifecycle: {stage_name}")
    for stage_name in stage_names:
        stage_override = overrides.get(stage_name)
        updates: list[str] = []
        params: list[int | str] = []
        max_work_attempts = (
            stage_override.max_work_attempts
            if stage_override and stage_override.max_work_attempts is not None
            else retry_cap
        )
        max_review_rounds = (
            stage_override.max_review_rounds
            if stage_override and stage_override.max_review_rounds is not None
            else retry_cap
        )
        if max_work_attempts is not None:
            updates.append(_STAGE_CAP_UPDATE_ASSIGNMENTS["max_work_attempts"])
            params.append(max_work_attempts)
        if max_review_rounds is not None:
            updates.append(_STAGE_CAP_UPDATE_ASSIGNMENTS["max_review_rounds"])
            params.append(max_review_rounds)
        if not updates:
            continue
        params.extend([task_id, stage_name])
        task_manager.db.execute(
            f"""
            UPDATE task_stage_states
               SET {", ".join(updates)}
             WHERE task_id = %s AND stage_name = %s
            """,
            tuple(params),
        )
