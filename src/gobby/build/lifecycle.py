"""Build lifecycle orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal

from gobby.build.delivery import record_build_delivery_campaign
from gobby.build.dispatch_tick import (
    kick_dispatcher_tick as _kick_dispatcher_tick,
)
from gobby.build.options import BuildOptions, retry_attempt_cap
from gobby.build.profiles import resolve_build_profile_options
from gobby.build.results import BuildResult
from gobby.build.stage_manifest import (
    AUTOMATED_LEAF_CATEGORIES,
    InputKind,
    _canonical_stage_name,
    _validate_skip_stages,
    resolve_stage_manifest_specs,
    specs_payload,
    stage_state_specs,
)
from gobby.build.target_branch import (
    _cascade_target_branch_to_subtree,
    _resolve_target_branch,
)
from gobby.build.validation import (
    _validate_clones_dir,
    _validate_epic_isolation_artifacts,
    _validate_max_active_agents,
    _validate_no_merge,
    _validate_retry_caps,
    _validate_task_ref_isolation_artifacts,
)
from gobby.build.workspaces import (
    ensure_epic_integration_workspaces,
    ensure_task_parent_integration_workspace,
)
from gobby.config.build import Isolation
from gobby.storage.build_history import (
    best_effort_finish_run,
    best_effort_record_event,
    best_effort_start_run,
)
from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks import (
    LocalTaskManager,
    ManifestAlreadyInitializedError,
    StageManifestSpec,
    Task,
)
from gobby.storage.tasks._lifecycle_events import BUILD_EVENT_REASON

_STAGE_CAP_UPDATE_ASSIGNMENTS = {
    "max_work_attempts": "max_work_attempts = ?",
    "max_review_rounds": "max_review_rounds = ?",
}


async def build(
    input_ref: str,
    opts: BuildOptions,
    *,
    db: DatabaseProtocol,
    project_id: str,
    services: object | None = None,
) -> BuildResult:
    """Start lifecycle automation for a plan file, epic, or automated leaf task."""
    run = best_effort_start_run(
        db,
        project_id=project_id,
        input_ref=input_ref,
        action="build",
        actor="build",
        summary={"quick": opts.quick, "isolation": opts.isolation},
    )
    try:
        result = await _build_impl(
            input_ref,
            opts,
            db=db,
            project_id=project_id,
            services=services,
        )
    except Exception as exc:
        best_effort_finish_run(
            db,
            run.id if run is not None else None,
            status="failed",
            error=str(exc),
        )
        best_effort_record_event(
            db,
            run_id=run.id if run is not None else None,
            project_id=project_id,
            event_type="build_failed",
            action="build",
            message=str(exc),
            payload={"input_ref": input_ref},
        )
        raise
    best_effort_finish_run(
        db,
        run.id if run is not None else None,
        status="completed",
        root_task_id=result.task_id,
        summary=asdict(result),
    )
    best_effort_record_event(
        db,
        run_id=run.id if run is not None else None,
        project_id=project_id,
        root_task_id=result.task_id,
        task_id=result.task_id,
        event_type="build_completed",
        action="build",
        message="gobby build",
        payload=asdict(result),
    )
    return result


async def _build_impl(
    input_ref: str,
    opts: BuildOptions,
    *,
    db: DatabaseProtocol,
    project_id: str,
    services: object | None = None,
) -> BuildResult:
    """Start lifecycle automation after history instrumentation is installed."""

    opts = resolve_build_profile_options(opts, db=db, project_id=project_id)
    skip_stages = _validate_skip_stages(opts.skip_stages)
    warnings: list[str] = []
    task_manager = LocalTaskManager(db)
    input_kind, task_or_plan = _resolve_input(input_ref, task_manager, project_id)

    _validate_no_merge(opts)
    _validate_clones_dir(opts)
    _validate_retry_caps(opts)
    _validate_max_active_agents(opts)
    target_branch = await _resolve_target_branch(db, project_id, opts, input_kind)

    if input_kind == "plan_file":
        if not isinstance(task_or_plan, Path):
            raise TypeError("plan_file input did not resolve to a path")
        return await _build_plan_file(
            task_manager,
            task_or_plan,
            opts,
            skip_stages,
            warnings,
            project_id,
            target_branch,
            db,
            services,
        )

    if not isinstance(task_or_plan, Task):
        raise TypeError("task input did not resolve to a task")
    task = task_or_plan
    if task_manager.stage_states.list_for_task(task.id):
        return await _resume_existing_lifecycle(
            task_manager,
            task,
            opts,
            skip_stages,
            warnings,
            db,
            project_id,
            services,
            target_branch,
        )

    _prepare_task_ref_expansion_output(task_manager, task, opts)
    if input_kind == "leaf":
        return await _build_leaf(
            task_manager,
            task,
            opts,
            skip_stages,
            warnings,
            target_branch,
            db,
            project_id,
            services,
        )

    return await _build_epic(
        task_manager,
        task,
        opts,
        skip_stages,
        warnings,
        target_branch,
        db,
        project_id,
        services,
    )


async def _build_plan_file(
    task_manager: LocalTaskManager,
    plan_file: Path,
    opts: BuildOptions,
    skip_stages: list[str],
    warnings: list[str],
    project_id: str,
    target_branch: str | None,
    db: DatabaseProtocol,
    services: object | None,
) -> BuildResult:
    task = task_manager.create_task(
        project_id=project_id,
        title=f"Build {plan_file.name}",
        description=f"Lifecycle automation seeded from plan file: {plan_file}",
        task_type="epic",
        category="planning",
    )
    task_manager.update_task(
        task.id,
        allow_automation=True,
        unattended=opts.unattended,
        isolation=opts.isolation,
        assigned_agent=opts.assigned_agent,
    )
    task_manager.artifacts.set_artifacts_atomic(
        task.id,
        plan_file_path=str(plan_file),
        target_branch=target_branch,
    )
    record_build_delivery_campaign(db, project_id=project_id, task_id=task.id, opts=opts)
    specs = _initialize_stage_manifest(task_manager, task, opts, skip_stages, "plan_file")
    initial_lifecycle = _current_stage_name(task_manager, task.id, specs)
    _record_build_event(task_manager, task.id, initial_lifecycle)
    tick = await _kick_dispatcher_tick(
        task_manager.db,
        project_id,
        dispatcher_enabled=True,
        services=services,
        max_ticks=_quick_tick_limit(opts),
        max_active_agents=opts.max_active_agents,
    )
    if opts.quick:
        _set_automation_for_task_tree(task_manager, task, False, isolation=opts.isolation)
    return BuildResult(
        task_id=task.id,
        created=True,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=skip_stages,
        tick_dispatched=tick.ticks,
        dispatcher_tick=tick,
        manifest=specs_payload(specs),
        warnings=warnings,
    )


async def _build_leaf(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    warnings: list[str],
    target_branch: str | None,
    db: DatabaseProtocol,
    project_id: str,
    services: object | None,
) -> BuildResult:
    if task.category not in AUTOMATED_LEAF_CATEGORIES:
        allowed = ", ".join(sorted(AUTOMATED_LEAF_CATEGORIES))
        raise ValueError(
            f"category {task.category} cannot be automated; expected one of: {allowed}"
        )
    _validate_task_ref_isolation_artifacts(task_manager, task, opts.isolation)

    task_manager.update_task(
        task.id,
        allow_automation=True,
        unattended=opts.unattended,
        isolation=opts.isolation,
        assigned_agent=opts.assigned_agent,
    )
    if opts.isolation in {"worktree", "clone"} and target_branch:
        task_manager.artifacts.set_artifact(task.id, "target_branch", target_branch)
    record_build_delivery_campaign(db, project_id=project_id, task_id=task.id, opts=opts)
    specs = _initialize_stage_manifest(task_manager, task, opts, skip_stages, "leaf")
    initial_lifecycle = _current_stage_name(task_manager, task.id, specs)
    _record_build_event(task_manager, task.id, initial_lifecycle)
    tick = await _kick_dispatcher_tick(
        db,
        project_id,
        dispatcher_enabled=True,
        services=services,
        max_ticks=_quick_tick_limit(opts),
        max_active_agents=opts.max_active_agents,
    )
    if opts.quick:
        _set_automation_for_task_tree(task_manager, task, False, isolation=opts.isolation)
    return BuildResult(
        task_id=task.id,
        created=False,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=skip_stages,
        tick_dispatched=tick.ticks,
        dispatcher_tick=tick,
        manifest=specs_payload(specs),
        warnings=warnings,
    )


async def _build_epic(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    warnings: list[str],
    target_branch: str | None,
    db: DatabaseProtocol,
    project_id: str,
    services: object | None,
) -> BuildResult:
    artifacts = task_manager.artifacts.get_artifacts(task.id)
    _validate_epic_isolation_artifacts(opts.isolation, artifacts)
    task_manager.artifacts.set_artifacts_atomic(
        task.id,
        target_branch=target_branch,
    )
    record_build_delivery_campaign(db, project_id=project_id, task_id=task.id, opts=opts)
    if opts.isolation in {"worktree", "clone"}:
        if target_branch is None:
            raise ValueError("target_branch is required for epic integration workspaces")
        await asyncio.to_thread(
            ensure_epic_integration_workspaces,
            task_manager=task_manager,
            root_task=task,
            backend=opts.workspace_backend,
            target_branch=target_branch,
            project_id=project_id,
            services=services,
        )
    specs = _initialize_stage_manifest(task_manager, task, opts, skip_stages, "epic")
    cascade_specs = (
        resolve_stage_manifest_specs(
            task_manager,
            task,
            "epic",
            replace(opts, no_merge=False),
            skip_stages,
        )
        if opts.no_merge
        else specs
    )
    task_manager.cascade_build_state_to_subtree(
        task.id,
        isolation=opts.isolation,
        unattended=opts.unattended,
        skip_stages=skip_stages,
        allow_automation=True,
        parent_manifest_specs=cascade_specs,
        include_merge_stage=opts.isolation in {"worktree", "clone"} and not opts.no_merge,
    )
    if opts.isolation == "none":
        _cascade_target_branch_to_subtree(task_manager, task.id, target_branch)
    initial_lifecycle = _current_stage_name(task_manager, task.id, specs)
    _record_build_event(task_manager, task.id, initial_lifecycle)
    tick = await _kick_dispatcher_tick(
        db,
        project_id,
        dispatcher_enabled=True,
        services=services,
        max_ticks=_quick_tick_limit(opts),
        max_active_agents=opts.max_active_agents,
    )
    if opts.quick:
        _set_automation_for_task_tree(task_manager, task, False, isolation=opts.isolation)
    return BuildResult(
        task_id=task.id,
        created=False,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=skip_stages,
        tick_dispatched=tick.ticks,
        dispatcher_tick=tick,
        manifest=specs_payload(specs),
        warnings=warnings,
    )


def _resolve_input(
    input_ref: str,
    task_manager: LocalTaskManager,
    project_id: str,
) -> tuple[InputKind, Task | Path]:
    if _looks_like_task_ref(input_ref):
        task = task_manager.get_task(input_ref, project_id=project_id)
        return ("epic" if task.task_type == "epic" else "leaf", task)

    plan_file = Path(input_ref)
    if not plan_file.exists() or not plan_file.is_file():
        raise ValueError(f"plan file not found: {input_ref}")
    return "plan_file", plan_file


async def _resume_existing_lifecycle(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    warnings: list[str],
    db: DatabaseProtocol,
    project_id: str,
    services: object | None,
    target_branch: str | None,
) -> BuildResult:
    if skip_stages:
        if opts.skip_stages_explicit:
            raise ValueError(
                "--skip-stage can only shape a new lifecycle; use build restart or clean first"
            )
        warnings.append("Profile skip_stages ignored because the task already has a manifest")
    resume_opts = opts
    _apply_stage_caps_to_existing_lifecycle(task_manager, task.id, resume_opts)
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
    if task.task_type == "epic":
        artifacts = task_manager.artifacts.get_artifacts(task.id)
        integration_target = target_branch or artifacts.target_branch
        if resume_opts.isolation in {"worktree", "clone"}:
            if integration_target is None:
                raise ValueError("target_branch is required for epic integration workspaces")
            await asyncio.to_thread(
                ensure_epic_integration_workspaces,
                task_manager=task_manager,
                root_task=task,
                backend=resume_opts.workspace_backend,
                target_branch=integration_target,
                project_id=project_id,
                services=services,
            )
        task_manager.cascade_build_state_to_subtree(
            task.id,
            isolation=resume_opts.isolation,
            unattended=opts.unattended,
            allow_automation=True,
            include_merge_stage=resume_opts.isolation in {"worktree", "clone"}
            and not opts.no_merge,
        )
    elif resume_opts.isolation in {"worktree", "clone"}:
        await asyncio.to_thread(
            ensure_task_parent_integration_workspace,
            task_manager=task_manager,
            task=task,
            backend=resume_opts.workspace_backend,
            project_id=project_id,
            services=services,
            base_branch_override=target_branch,
        )
    specs = stage_state_specs(task_manager, task.id)
    initial_lifecycle = _current_stage_name(task_manager, task.id, specs)
    _record_build_event(task_manager, task.id, initial_lifecycle)
    tick = await _kick_dispatcher_tick(
        db,
        project_id,
        dispatcher_enabled=True,
        services=services,
        max_ticks=_quick_tick_limit(opts),
        max_active_agents=opts.max_active_agents,
    )
    if opts.quick:
        _set_automation_for_task_tree(task_manager, task, False, isolation=resume_opts.isolation)
    return BuildResult(
        task_id=task.id,
        created=False,
        initial_lifecycle=initial_lifecycle,
        applied_stages_skipped=[],
        tick_dispatched=tick.ticks,
        dispatcher_tick=tick,
        manifest=specs_payload(specs),
        warnings=warnings,
    )


def _apply_stage_caps_to_existing_lifecycle(
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
             WHERE task_id = ? AND stage_name = ?
            """,
            tuple(params),
        )


def _quick_tick_limit(opts: BuildOptions) -> int | None:
    return 2 if opts.quick else None


def _set_automation_for_task_tree(
    task_manager: LocalTaskManager,
    task: Task,
    enabled: bool,
    *,
    isolation: Isolation,
) -> None:
    if task.task_type != "epic":
        task_manager.update_task(task.id, allow_automation=enabled)
        return
    task_manager.cascade_build_state_to_subtree(
        task.id,
        isolation=isolation,
        unattended=False,
        allow_automation=enabled,
    )


def _prepare_task_ref_expansion_output(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
) -> None:
    from gobby.tasks.expansion_service import ExpansionService

    service = ExpansionService(task_manager=task_manager, llm_service=None)
    if opts.reset_expansion_output:
        service.reset_expansion_output(task.id)
        return
    existing = service.find_existing_expansion_output(task.id)
    if existing is not None:
        raise ValueError(
            "Expansion output already exists for this task. "
            "Use --reset-expansion-output before rebuilding."
        )


def _current_stage_name(
    task_manager: LocalTaskManager,
    task_id: str,
    specs: list[StageManifestSpec],
) -> str:
    current = task_manager.stage_states.current_stage(task_id)
    if current is not None:
        return current.stage_name
    if not specs:
        raise ValueError(f"stage manifest is empty for task {task_id}")
    return min(specs, key=lambda spec: spec.position).stage_name


def _record_build_event(
    task_manager: LocalTaskManager,
    task_id: str,
    to_state: str,
) -> None:
    task_manager.lifecycle_events.record_lifecycle_event(
        task_id,
        from_state=None,
        to_state=to_state,
        reason=BUILD_EVENT_REASON,
        by_actor="build",
    )


BuildState = Literal["never_started", "running", "paused"]


def derive_build_state(*, allow_automation: bool, has_build_event: bool) -> BuildState:
    """Resolve a task's definitive build state for the web payload.

    - ``running``: automation is currently enabled (``allow_automation``).
    - ``paused``: automation is off but a build was started at some point
      (``build_stop_target`` clears ``allow_automation`` without recording a
      new lifecycle event or bumping ``dispatch_failure_count``, so the durable
      ``gobby build`` event is the only honest signal here).
    - ``never_started``: automation is off and no build was ever started.
    """
    if allow_automation:
        return "running"
    if has_build_event:
        return "paused"
    return "never_started"


def _initialize_stage_manifest(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    input_kind: InputKind,
) -> list[StageManifestSpec]:
    specs = resolve_stage_manifest_specs(task_manager, task, input_kind, opts, skip_stages)
    try:
        task_manager.stage_states.initialize_manifest(task.id, specs, by_session_id=None)
    except ManifestAlreadyInitializedError as exc:
        raise ValueError(
            "Task already has a different lifecycle manifest. "
            f"Use `gobby build restart {task.id}` or `gobby build clean {task.id}` "
            "before changing the build stage shape."
        ) from exc
    return specs


def _looks_like_task_ref(input_ref: str) -> bool:
    return input_ref.startswith("#") or input_ref.isdigit()


__all__ = [
    "BuildState",
    "build",
    "derive_build_state",
]
