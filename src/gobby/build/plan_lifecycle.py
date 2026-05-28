"""Lifecycle setup for plan-file build inputs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from gobby.build.delivery import record_build_delivery_campaign
from gobby.build.lifecycle_state import (
    current_stage_name,
    initialize_stage_manifest,
    record_build_event,
)
from gobby.build.options import BuildOptions
from gobby.build.results import BuildResult
from gobby.build.runtime_hooks import RuntimeHooks
from gobby.build.stage_manifest import specs_payload
from gobby.build.task_lifecycle import set_automation_for_task_tree
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager


async def build_plan_file(
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
    *,
    runtime: RuntimeHooks,
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
    specs = initialize_stage_manifest(task_manager, task, opts, skip_stages, "plan_file")
    seed_plan_file_stage_state(task_manager, task.id, opts)
    initial_lifecycle = current_stage_name(task_manager, task.id, specs)
    record_build_event(task_manager, task.id, initial_lifecycle)
    runtime.attach_build_run_root(db, build_run_id, task.id)
    tick = await runtime.build_dispatcher_tick(
        task_manager.db,
        project_id,
        opts,
        dispatcher_enabled=True,
        services=services,
        runtime=runtime,
    )
    if opts.quick:
        set_automation_for_task_tree(task_manager, task, False, isolation=opts.isolation)
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


def seed_plan_file_stage_state(
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
