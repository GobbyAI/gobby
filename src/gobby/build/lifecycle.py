"""Build lifecycle orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from gobby.build.delivery import record_build_delivery_campaign
from gobby.build.dispatch_tick import (
    DispatcherTickSummary,
)
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
    _validate_planning_seed,
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
    best_effort_update_run_context,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import (
    LocalTaskManager,
    ManifestAlreadyInitializedError,
    StageManifestSpec,
    StageState,
    Task,
)
from gobby.storage.tasks._lifecycle_events import BUILD_EVENT_REASON
from gobby.utils.sql import sql_placeholders

_EXPANDED_EPIC_LEGACY_ROOT_STAGES = frozenset(
    {"ideation", "research", "architecture", "prd", "planning", "expansion", "pr"}
)

_STAGE_CAP_UPDATE_ASSIGNMENTS = {
    "max_work_attempts": "max_work_attempts = %s",
    "max_review_rounds": "max_review_rounds = %s",
}

_DRY_RUN_PLAN_TASK_ID = "dry-run:plan-file"


class _DryRunRollback(Exception):
    """Internal sentinel used to roll back dry-run preview writes."""


async def build(
    input_ref: str,
    opts: BuildOptions,
    *,
    db: HubDatabase,
    project_id: str,
    services: object | None = None,
) -> BuildResult:
    """Start lifecycle automation for a plan file, epic, or automated leaf task."""
    coordinator_session_id = _resolve_coordinator_session_id(
        opts,
        db=db,
        project_id=project_id,
        services=services,
    )
    if opts.dry_run:
        return await _build_dry_run(
            input_ref,
            opts,
            db=db,
            project_id=project_id,
            services=services,
        )

    run = best_effort_start_run(
        db,
        project_id=project_id,
        input_ref=input_ref,
        action="build",
        actor="build",
        summary=_build_run_summary(
            {"quick": opts.quick, "isolation": opts.isolation},
            coordinator_session_id,
        ),
    )
    try:
        result = await _build_impl(
            input_ref,
            opts,
            db=db,
            project_id=project_id,
            services=services,
            build_run_id=run.id if run is not None else None,
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
        summary=_build_run_summary(asdict(result), coordinator_session_id),
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


async def _build_dry_run(
    input_ref: str,
    opts: BuildOptions,
    *,
    db: HubDatabase,
    project_id: str,
    services: object | None = None,
) -> BuildResult:
    result: BuildResult | None = None
    try:
        with db.transaction_immediate():
            result = await _build_impl(
                input_ref,
                opts,
                db=db,
                project_id=project_id,
                services=services,
            )
            raise _DryRunRollback
    except _DryRunRollback:
        if result is None:
            raise RuntimeError("dry-run build did not produce a result") from None
        if result.created:
            result = replace(result, task_id=_DRY_RUN_PLAN_TASK_ID)
        return replace(result, dry_run=True)


def _build_run_summary(
    payload: dict[str, object],
    coordinator_session_id: str | None,
) -> dict[str, object]:
    if coordinator_session_id is None:
        return payload
    return {**payload, "coordinator_session_id": coordinator_session_id}


def _resolve_coordinator_session_id(
    opts: BuildOptions,
    *,
    db: HubDatabase,
    project_id: str,
    services: object | None,
) -> str | None:
    ref = opts.coordinator_session_ref
    if not ref:
        return None
    manager = getattr(services, "session_manager", None) or SessionManager(db)
    try:
        resolved_id = str(manager.resolve_session_reference(ref, project_id))
    except ValueError as exc:
        raise ValueError(f"build coordinator session could not be resolved: {exc}") from exc
    session = manager.get(resolved_id)
    if session is None:
        raise ValueError(f"build coordinator session not found: {ref}")
    if session.project_id != project_id:
        raise ValueError("build coordinator session must belong to the build project")
    return resolved_id


def _attach_build_run_root(
    db: HubDatabase,
    build_run_id: str | None,
    root_task_id: str,
) -> None:
    best_effort_update_run_context(db, build_run_id, root_task_id=root_task_id)


async def _build_impl(
    input_ref: str,
    opts: BuildOptions,
    *,
    db: HubDatabase,
    project_id: str,
    services: object | None = None,
    build_run_id: str | None = None,
) -> BuildResult:
    """Start lifecycle automation after history instrumentation is installed."""

    opts = resolve_build_profile_options(opts, db=db, project_id=project_id)
    skip_stages = _validate_skip_stages(opts.skip_stages)
    warnings: list[str] = []
    task_manager = LocalTaskManager(db)
    input_kind, task_or_plan = _resolve_input(input_ref, task_manager, project_id, opts)

    _validate_no_merge(opts)
    _validate_clones_dir(opts)
    _validate_retry_caps(opts)
    _validate_max_active_agents(opts)
    _validate_planning_seed(opts)
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
            build_run_id,
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
            build_run_id,
        )

    _reset_task_ref_expansion_output(task_manager, task, opts)
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
            build_run_id,
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
        build_run_id,
    )


async def _build_plan_file(
    task_manager: LocalTaskManager,
    plan_file: Path,
    opts: BuildOptions,
    skip_stages: list[str],
    warnings: list[str],
    project_id: str,
    target_branch: str | None,
    db: HubDatabase,
    services: object | None,
    build_run_id: str | None,
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
    _seed_plan_file_stage_state(task_manager, task.id, opts)
    initial_lifecycle = _current_stage_name(task_manager, task.id, specs)
    _record_build_event(task_manager, task.id, initial_lifecycle)
    _attach_build_run_root(db, build_run_id, task.id)
    tick = await _build_dispatcher_tick(
        task_manager.db,
        project_id,
        opts,
        dispatcher_enabled=True,
        services=services,
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
        dry_run=opts.dry_run,
    )


def _seed_plan_file_stage_state(
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
                "Seeded plan review state from build input.",
                task_id,
            ),
        )


async def _build_leaf(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    warnings: list[str],
    target_branch: str | None,
    db: HubDatabase,
    project_id: str,
    services: object | None,
    build_run_id: str | None,
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
    _attach_build_run_root(db, build_run_id, task.id)
    tick = await _build_dispatcher_tick(
        db,
        project_id,
        opts,
        dispatcher_enabled=True,
        services=services,
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
        dry_run=opts.dry_run,
    )


async def _build_epic(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
    warnings: list[str],
    target_branch: str | None,
    db: HubDatabase,
    project_id: str,
    services: object | None,
    build_run_id: str | None,
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
        if not opts.dry_run:
            await asyncio.to_thread(
                ensure_epic_integration_workspaces,
                task_manager=task_manager,
                root_task=task,
                backend=opts.workspace_backend,
                target_branch=target_branch,
                project_id=project_id,
                services=services,
            )
    manifest_input_kind: InputKind = (
        "expanded_epic" if _has_existing_expansion_output(task_manager, task) else "epic"
    )
    specs = _initialize_stage_manifest(
        task_manager,
        task,
        opts,
        skip_stages,
        manifest_input_kind,
    )
    cascade_specs = (
        resolve_stage_manifest_specs(
            task_manager,
            task,
            manifest_input_kind,
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
    _attach_build_run_root(db, build_run_id, task.id)
    tick = await _build_dispatcher_tick(
        db,
        project_id,
        opts,
        dispatcher_enabled=True,
        services=services,
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
        dry_run=opts.dry_run,
    )


def _resolve_input(
    input_ref: str,
    task_manager: LocalTaskManager,
    project_id: str,
    opts: BuildOptions,
) -> tuple[InputKind, Task | Path]:
    if _looks_like_task_ref(input_ref):
        task = task_manager.get_task(input_ref, project_id=project_id)
        return ("epic" if task.task_type == "epic" else "leaf", task)

    plan_file = _resolve_plan_file_path(input_ref, task_manager, project_id, opts)
    if not plan_file.exists() or not plan_file.is_file():
        raise ValueError(f"plan file not found: {input_ref}")
    existing = _open_plan_file_build(task_manager, plan_file, project_id)
    if existing is not None:
        return "epic", existing
    return "plan_file", plan_file


def _open_plan_file_build(
    task_manager: LocalTaskManager,
    plan_file: Path,
    project_id: str,
) -> Task | None:
    candidates = _plan_file_path_candidates(plan_file)
    if not candidates:
        return None
    placeholders = sql_placeholders(len(candidates))
    row = task_manager.db.fetchone(
        f"""
        SELECT t.id
          FROM tasks t
          JOIN task_artifacts a ON a.task_id = t.id
         WHERE t.project_id = %s
           AND t.parent_task_id IS NULL
           AND t.task_type = 'epic'
           AND t.closed_at IS NULL
           AND a.plan_file_path IN ({placeholders})
         ORDER BY t.created_at ASC
         LIMIT 1
        """,  # nosec B608 # placeholder count is derived from normalized candidate paths.
        (project_id, *candidates),
    )
    if row is None:
        return None
    return task_manager.get_task(str(row["id"]), project_id=project_id)


def _plan_file_path_candidates(plan_file: Path) -> tuple[str, ...]:
    candidates = [str(plan_file)]
    try:
        candidates.append(str(plan_file.resolve()))
    except OSError:
        pass
    return tuple(dict.fromkeys(candidates))


def _resolve_plan_file_path(
    input_ref: str,
    task_manager: LocalTaskManager,
    project_id: str,
    opts: BuildOptions,
) -> Path:
    path = Path(input_ref).expanduser()
    if path.is_absolute():
        return path
    base_dir = _plan_file_base_dir(task_manager, project_id, opts)
    direct_path = base_dir / path
    if direct_path.exists() or path.parent != Path("."):
        return direct_path
    plans_path = base_dir / ".gobby" / "plans" / path.name
    if plans_path.exists():
        return plans_path
    return direct_path


def _plan_file_base_dir(
    task_manager: LocalTaskManager,
    project_id: str,
    opts: BuildOptions,
) -> Path:
    if opts.cwd is not None:
        return opts.cwd.expanduser()
    project = LocalProjectManager(task_manager.db).get(project_id)
    if project is not None and project.repo_path:
        return Path(project.repo_path).expanduser()
    return Path.cwd()


async def _resume_existing_lifecycle(
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
) -> BuildResult:
    resume_skip_stages = skip_stages
    skip_stages_shape_resume = _skip_stages_can_shape_expanded_epic_resume(
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
    _repair_expanded_epic_root_manifest_for_resume(
        task_manager,
        task,
        resume_opts,
        resume_skip_stages,
    )
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
        task_manager.cascade_build_state_to_subtree(
            task.id,
            isolation=resume_opts.isolation,
            unattended=opts.unattended,
            allow_automation=True,
            include_merge_stage=resume_opts.isolation in {"worktree", "clone"}
            and not opts.no_merge,
        )
        if resume_opts.isolation in {"worktree", "clone"}:
            if integration_target is None:
                raise ValueError("target_branch is required for epic integration workspaces")
            if not resume_opts.dry_run:
                await asyncio.to_thread(
                    ensure_epic_integration_workspaces,
                    task_manager=task_manager,
                    root_task=task,
                    backend=resume_opts.workspace_backend,
                    target_branch=integration_target,
                    project_id=project_id,
                    services=services,
                )
    elif resume_opts.isolation in {"worktree", "clone"} and not resume_opts.dry_run:
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
    _attach_build_run_root(db, build_run_id, task.id)
    tick = await _build_dispatcher_tick(
        db,
        project_id,
        opts,
        dispatcher_enabled=True,
        services=services,
    )
    if opts.quick:
        _set_automation_for_task_tree(task_manager, task, False, isolation=resume_opts.isolation)
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


def _skip_stages_can_shape_expanded_epic_resume(
    task_manager: LocalTaskManager,
    task: Task,
    skip_stages: list[str],
) -> bool:
    if not skip_stages:
        return False
    return (
        task.task_type == "epic"
        and set(skip_stages) <= {"pr"}
        and _has_existing_expansion_output(task_manager, task)
    )


def _repair_expanded_epic_root_manifest_for_resume(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
    skip_stages: list[str],
) -> bool:
    if task.task_type != "epic" or not _has_existing_expansion_output(task_manager, task):
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
    if not all(_is_pristine_resume_stage(row) for row in desired_rows):
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


def _is_pristine_resume_stage(row: StageState) -> bool:
    return (
        row.state == "ready"
        and row.entered_at is None
        and row.completed_at is None
        and row.work_attempt_count == 0
        and row.review_round_count == 0
        and row.artifact_refs is None
        and row.notes is None
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
             WHERE task_id = %s AND stage_name = %s
            """,
            tuple(params),
        )


def _quick_tick_limit(opts: BuildOptions) -> int | None:
    return 1 if opts.quick else None


def _quick_action_limit(opts: BuildOptions) -> int | None:
    return 1 if opts.quick else None


async def _build_dispatcher_tick(
    db: HubDatabase,
    project_id: str,
    opts: BuildOptions,
    *,
    dispatcher_enabled: bool,
    services: object | None,
) -> DispatcherTickSummary:
    if opts.dry_run:
        return DispatcherTickSummary(reason="dry_run")
    return await _kick_dispatcher_tick(
        db,
        project_id,
        dispatcher_enabled=dispatcher_enabled,
        services=services,
        max_ticks=_quick_tick_limit(opts),
        max_actions=_quick_action_limit(opts),
        max_active_agents=opts.max_active_agents,
    )


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


def _reset_task_ref_expansion_output(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
) -> None:
    if not opts.reset_expansion_output:
        return
    from gobby.tasks.expansion_service import ExpansionService

    service = ExpansionService(task_manager=task_manager, llm_service=None)
    service.reset_expansion_output(task.id)


def _has_existing_expansion_output(task_manager: LocalTaskManager, task: Task) -> bool:
    if task.task_type != "epic":
        return False

    from gobby.tasks.expansion_service import ExpansionService

    service = ExpansionService(task_manager=task_manager, llm_service=None)
    return service.find_existing_expansion_output(task.id) is not None


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
