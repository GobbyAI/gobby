"""Build lifecycle orchestration."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

from gobby.build.coordinator import build_run_summary, resolve_build_coordinator
from gobby.build.dispatch_tick import DispatcherTickSummary
from gobby.build.dispatch_tick import kick_dispatcher_tick as _kick_dispatcher_tick
from gobby.build.input_resolution import resolve_input as _resolve_input
from gobby.build.lifecycle_state import BuildState, derive_build_state
from gobby.build.options import BuildOptions
from gobby.build.plan_lifecycle import build_plan_file as _build_plan_file
from gobby.build.profiles import resolve_build_profile_options
from gobby.build.project_controls import build_resume
from gobby.build.project_state import is_project_automation_enabled
from gobby.build.results import BuildResult
from gobby.build.resume_lifecycle import resume_existing_lifecycle as _resume_existing_lifecycle
from gobby.build.runtime_hooks import RuntimeHooks
from gobby.build.stage_manifest import _validate_skip_stages
from gobby.build.target_branch import _resolve_target_branch
from gobby.build.task_lifecycle import (
    build_epic as _build_epic,
)
from gobby.build.task_lifecycle import (
    build_leaf as _build_leaf,
)
from gobby.build.task_lifecycle import (
    reset_task_ref_expansion_output as _reset_task_ref_expansion_output,
)
from gobby.build.validation import (
    _validate_clones_dir,
    _validate_max_active_agents,
    _validate_no_merge,
    _validate_planning_seed,
    _validate_retry_caps,
)
from gobby.build.workspaces import (
    ensure_epic_integration_workspaces,
    ensure_task_parent_integration_workspace,
)
from gobby.storage.build_history import (
    best_effort_finish_run,
    best_effort_record_event,
    best_effort_start_run,
    best_effort_update_run_context,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task

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
    coordinator = resolve_build_coordinator(
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
        summary=build_run_summary(
            {"quick": opts.quick, "isolation": opts.isolation},
            coordinator=coordinator,
            build_project_id=project_id,
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
        summary=build_run_summary(
            asdict(result),
            coordinator=coordinator,
            build_project_id=project_id,
        ),
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


def _attach_build_run_root(
    db: HubDatabase,
    build_run_id: str | None,
    root_task_id: str,
) -> None:
    best_effort_update_run_context(db, build_run_id, root_task_id=root_task_id)


def _runtime_hooks() -> RuntimeHooks:
    return RuntimeHooks(
        dispatcher_tick=_kick_dispatcher_tick,
        ensure_epic_integration_workspaces=ensure_epic_integration_workspaces,
        ensure_task_parent_integration_workspace=ensure_task_parent_integration_workspace,
        build_dispatcher_tick=_build_dispatcher_tick,
        attach_build_run_root=_attach_build_run_root,
    )


def _ensure_launch_automation_enabled(
    db: HubDatabase,
    project_id: str,
    warnings: list[str],
) -> None:
    if is_project_automation_enabled(db, project_id):
        return
    build_resume(db=db, project_id=project_id)
    warnings.append("Project build automation was paused; resumed before initial dispatch.")


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
    runtime = _runtime_hooks()

    _validate_no_merge(opts)
    _validate_clones_dir(opts)
    _validate_retry_caps(opts)
    _validate_max_active_agents(opts)
    _validate_planning_seed(opts)
    target_branch = await _resolve_target_branch(db, project_id, opts, input_kind)
    _ensure_launch_automation_enabled(db, project_id, warnings)

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
            runtime=runtime,
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
            runtime=runtime,
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
            runtime=runtime,
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
        runtime=runtime,
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
    runtime: RuntimeHooks,
) -> DispatcherTickSummary:
    if opts.dry_run:
        return DispatcherTickSummary(reason="dry_run")
    return await runtime.dispatcher_tick(
        db,
        project_id,
        dispatcher_enabled=dispatcher_enabled,
        services=services,
        max_ticks=_quick_tick_limit(opts),
        max_actions=_quick_action_limit(opts),
        max_active_agents=opts.max_active_agents,
    )


__all__ = [
    "BuildState",
    "build",
    "derive_build_state",
]
