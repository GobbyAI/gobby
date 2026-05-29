"""Lifecycle setup for task-ref build inputs."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from gobby.build.delivery import record_build_delivery_campaign
from gobby.build.lifecycle_state import (
    current_stage_name,
    initialize_stage_manifest,
    record_build_event,
)
from gobby.build.options import BuildOptions
from gobby.build.results import BuildResult
from gobby.build.runtime_hooks import RuntimeHooks
from gobby.build.stage_manifest import (
    AUTOMATED_LEAF_CATEGORIES,
    InputKind,
    resolve_stage_manifest_specs,
    specs_payload,
)
from gobby.build.target_branch import _cascade_target_branch_to_subtree
from gobby.build.validation import (
    _validate_epic_isolation_artifacts,
    _validate_task_ref_isolation_artifacts,
)
from gobby.config.build import Isolation
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task


async def build_leaf(
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
    *,
    runtime: RuntimeHooks,
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
    specs = initialize_stage_manifest(task_manager, task, opts, skip_stages, "leaf")
    initial_lifecycle = current_stage_name(task_manager, task.id, specs)
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
        set_automation_for_task_tree(task_manager, task, False, isolation=opts.isolation)
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


async def build_epic(
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
    *,
    runtime: RuntimeHooks,
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
                runtime.ensure_epic_integration_workspaces,
                task_manager=task_manager,
                root_task=task,
                backend=opts.workspace_backend,
                target_branch=target_branch,
                project_id=project_id,
                services=services,
            )
    manifest_input_kind: InputKind = (
        "expanded_epic" if has_existing_expansion_output(task_manager, task) else "epic"
    )
    specs = initialize_stage_manifest(
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
    if not opts.dry_run:
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
    initial_lifecycle = current_stage_name(task_manager, task.id, specs)
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
        set_automation_for_task_tree(task_manager, task, False, isolation=opts.isolation)
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


def set_automation_for_task_tree(
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


def reset_task_ref_expansion_output(
    task_manager: LocalTaskManager,
    task: Task,
    opts: BuildOptions,
) -> None:
    if not opts.reset_expansion_output:
        return
    from gobby.tasks.expansion_service import ExpansionService

    service = ExpansionService(task_manager=task_manager, llm_service=None)
    service.reset_expansion_output(task.id)


def has_existing_expansion_output(task_manager: LocalTaskManager, task: Task) -> bool:
    if task.task_type != "epic":
        return False

    from gobby.tasks.expansion_service import ExpansionService

    service = ExpansionService(task_manager=task_manager, llm_service=None)
    return service.find_existing_expansion_output(task.id) is not None
