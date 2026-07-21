"""List and report task CLI command implementations."""

import click

from gobby.cli.tasks._crud_common import current_stage_display
from gobby.cli.tasks._crud_services import CrudServices
from gobby.tasks.state_semantics import is_task_closed, serialize_task_state
from gobby.utils.json_helpers import json_dumps


def list_tasks_impl(
    services: CrudServices,
    active: bool,
    project_ref: str | None,
    stage_name: str | None,
    stage_state: str | None,
    claimed: bool,
    unclaimed: bool,
    ready: bool,
    blocked: bool,
    closed_only: bool,
    escalated: bool,
    limit: int,
    group_by: str | None,
    json_format: bool,
) -> None:
    if ready and blocked:
        click.echo("Error: --ready and --blocked are mutually exclusive.", err=True)
        return

    if claimed and unclaimed:
        click.echo("Error: --claimed and --unclaimed are mutually exclusive.", err=True)
        return

    if stage_state and not stage_name:
        click.echo("Error: --state requires --stage.", err=True)
        return

    if sum(bool(flag) for flag in (active, closed_only, escalated)) > 1:
        click.echo("Error: --active, --closed, and --escalated are mutually exclusive.", err=True)
        return

    if (ready or blocked) and any((active, claimed, unclaimed, closed_only, escalated, stage_name)):
        click.echo(
            "Error: --ready/--blocked cannot be combined with --active, --claimed, "
            "--unclaimed, --closed, --escalated, or --stage.",
            err=True,
        )
        return

    claimed_filter: bool | None = None
    if claimed:
        claimed_filter = True
    elif unclaimed:
        claimed_filter = False

    closed_filter: bool | None = None
    if closed_only:
        closed_filter = True
    elif active or escalated:
        closed_filter = False

    project_id = services.resolve_project_ref(project_ref)
    manager = services.get_task_manager()

    if ready:
        tasks_list = manager.list_ready_tasks(
            project_id=project_id,
            limit=limit,
        )
        label = "ready tasks"
        empty_msg = "No ready tasks found."
    elif blocked:
        tasks_list = manager.list_blocked_tasks(
            project_id=project_id,
            limit=limit,
        )
        label = "blocked tasks"
        empty_msg = "No blocked tasks found."
    else:
        tasks_list = manager.list_tasks(
            project_id=project_id,
            claimed=claimed_filter,
            closed=closed_filter,
            escalated=True if escalated else None,
            limit=10000 if stage_name else limit,
        )
        tasks_list = services.filter_tasks_by_stage(
            manager,
            tasks_list,
            stage_name=stage_name,
            state=stage_state,
            project_id=project_id,
        )[:limit]
        if closed_filter:
            label = "closed tasks"
        elif escalated:
            label = "escalated tasks"
        elif claimed_filter is True:
            label = "claimed tasks"
        elif claimed_filter is False:
            label = "unclaimed tasks"
        elif active:
            label = "active tasks"
        elif stage_name:
            label = "stage-filtered tasks"
        else:
            label = "tasks"
        empty_msg = "No tasks found."

    if json_format:
        click.echo(json_dumps([t.to_dict() for t in tasks_list], indent=2, default=str))
        return

    if not tasks_list:
        click.echo(empty_msg)
        return

    primary_ids: set[str] | None = None
    display_tasks = tasks_list
    if (
        ready
        or blocked
        or stage_name
        or escalated
        or claimed_filter is not None
        or closed_filter is not None
    ):
        display_tasks, primary_ids = services.collect_ancestors(tasks_list, manager)

    display_tasks = services.sort_tasks_for_tree(display_tasks)
    claimed_owner_map = services.get_claimed_task_owners(manager.db)
    claimed_ids = set(claimed_owner_map)

    effective_group_by = group_by
    if effective_group_by is None and project_id is None:
        effective_group_by = "project"

    prefixes = (
        services.compute_tree_prefixes(display_tasks, primary_ids)
        if effective_group_by != "stage"
        else None
    )
    click.echo(f"Found {len(tasks_list)} {label}:")
    rendered = services.format_task_list(
        display_tasks,
        claimed_task_ids=claimed_ids,
        claimed_task_owner_map=claimed_owner_map,
        primary_ids=primary_ids,
        tree_prefixes=prefixes,
        group_by=effective_group_by,
        db=manager.db,
    )
    if rendered:
        click.echo(rendered)


def ready_tasks_impl(
    services: CrudServices,
    limit: int,
    project_ref: str | None,
    priority: int | None,
    task_type: str | None,
    json_format: bool,
    flat: bool,
) -> None:
    project_id = services.resolve_project_ref(project_ref)
    manager = services.get_task_manager()
    tasks_list = manager.list_ready_tasks(
        project_id=project_id,
        priority=priority,
        task_type=task_type,
        limit=limit,
    )

    if json_format:
        click.echo(json_dumps([t.to_dict() for t in tasks_list], indent=2, default=str))
        return

    if not tasks_list:
        click.echo("No ready tasks found.")
        return

    claimed_owner_map = services.get_claimed_task_owners(manager.db)
    claimed_ids = set(claimed_owner_map)

    click.echo(f"Found {len(tasks_list)} ready tasks:")

    if flat:
        rendered = services.format_task_list(
            list(tasks_list),
            claimed_task_ids=claimed_ids,
            claimed_task_owner_map=claimed_owner_map,
            db=manager.db,
        )
    else:
        display_tasks, primary_ids = services.collect_ancestors(tasks_list, manager)
        display_tasks = services.sort_tasks_for_tree(display_tasks)
        prefixes = services.compute_tree_prefixes(display_tasks, primary_ids)
        rendered = services.format_task_list(
            display_tasks,
            claimed_task_ids=claimed_ids,
            claimed_task_owner_map=claimed_owner_map,
            primary_ids=primary_ids,
            tree_prefixes=prefixes,
            db=manager.db,
        )
    if rendered:
        click.echo(rendered)


def blocked_tasks_impl(
    services: CrudServices, limit: int, project_ref: str | None, json_format: bool
) -> None:
    from gobby.storage.task_dependencies import TaskDependencyManager

    project_id = services.resolve_project_ref(project_ref)
    manager = services.get_task_manager()
    dep_manager = TaskDependencyManager(manager.db)
    blocked_list = manager.list_blocked_tasks(project_id=project_id, limit=limit)

    if json_format:
        result = []
        for task in blocked_list:
            tree = dep_manager.get_dependency_tree(task.id)
            result.append(
                {
                    "task": task.to_dict(),
                    "blocked_by": tree.get("blockers", []),
                }
            )
        click.echo(json_dumps(result, indent=2, default=str))
        return

    if not blocked_list:
        click.echo("No blocked tasks found.")
        return

    click.echo(f"Found {len(blocked_list)} blocked tasks:")
    for task in blocked_list:
        tree = dep_manager.get_dependency_tree(task.id)
        blocker_ids = tree.get("blockers", [])
        click.echo(f"\n○ {task.id[:8]}: {task.title}")
        if blocker_ids:
            click.echo("  Blocked by:")
            for b in blocker_ids:
                blocker_id = b.get("id") if isinstance(b, dict) else b
                if not blocker_id or not isinstance(blocker_id, str):
                    continue

                bid: str = blocker_id

                try:
                    blocker_task = manager.get_task(bid)
                    status_icon = "✓" if is_task_closed(blocker_task) else "○"
                    click.echo(f"    {status_icon} {bid[:8]}: {blocker_task.title}")
                except Exception:
                    click.echo(f"    ? {bid[:8]}: (not found)")


def task_stats_impl(services: CrudServices, project_ref: str | None, json_format: bool) -> None:
    project_id = services.resolve_project_ref(project_ref)
    manager = services.get_task_manager()

    all_tasks = manager.list_tasks(project_id=project_id, limit=10000)
    total = len(all_tasks)
    by_stage_state: dict[str, int] = {}
    by_priority = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    other_priority_count = 0
    by_type: dict[str, int] = {}
    claimed_count = 0
    unclaimed_count = 0
    closed_count = 0
    escalated_count = 0
    merge_ready_count = 0

    for task in all_tasks:
        state = serialize_task_state(task)
        if state["is_closed"]:
            closed_count += 1
        else:
            stage_display = current_stage_display(state)
            by_stage_state[stage_display] = by_stage_state.get(stage_display, 0) + 1
            if state["is_claimed"]:
                claimed_count += 1
            else:
                unclaimed_count += 1
            if state["is_escalated"]:
                escalated_count += 1
            if state["is_merge_ready"]:
                merge_ready_count += 1

        if task.priority in by_priority:
            by_priority[task.priority] = by_priority.get(task.priority, 0) + 1
        elif task.priority is not None:
            other_priority_count += 1
        if task.task_type:
            by_type[task.task_type] = by_type.get(task.task_type, 0) + 1

    ready_count = len(manager.list_ready_tasks(project_id=project_id, limit=10000))
    blocked_count = len(manager.list_blocked_tasks(project_id=project_id, limit=10000))

    stats = {
        "total": total,
        "by_stage_state": by_stage_state,
        "by_priority": {
            "critical": by_priority.get(0, 0),
            "high": by_priority.get(1, 0),
            "medium": by_priority.get(2, 0),
            "low": by_priority.get(3, 0),
            "backlog": by_priority.get(4, 0),
            "other": other_priority_count,
        },
        "by_type": by_type,
        "claimed": claimed_count,
        "unclaimed": unclaimed_count,
        "closed": closed_count,
        "escalated": escalated_count,
        "merge_ready": merge_ready_count,
        "ready": ready_count,
        "blocked": blocked_count,
    }

    if json_format:
        click.echo(json_dumps(stats, indent=2))
        return

    click.echo("Task Statistics:")
    click.echo(f"  Total: {total}")
    click.echo("  By Current Stage:")
    for stage_state, count in sorted(by_stage_state.items()):
        click.echo(f"    {stage_state}: {count}")
    click.echo(f"  Closed: {closed_count}")
    click.echo(f"\n  Claimed: {claimed_count}")
    click.echo(f"  Unclaimed Active: {unclaimed_count}")
    click.echo(f"  Escalated: {escalated_count}")
    click.echo(f"  Merge Ready: {merge_ready_count}")
    click.echo(f"\n  Ready (no blockers): {ready_count}")
    click.echo(f"  Blocked: {blocked_count}")
    click.echo(f"\n  Critical Priority: {by_priority.get(0, 0)}")
    click.echo(f"  High Priority: {by_priority.get(1, 0)}")
    click.echo(f"  Medium Priority: {by_priority.get(2, 0)}")
    click.echo(f"  Low Priority: {by_priority.get(3, 0)}")
    click.echo(f"  Backlog Priority: {by_priority.get(4, 0)}")
    click.echo(f"  Other Priority: {other_priority_count}")
    if by_type:
        click.echo("\n  By Type:")
        for t, count in sorted(by_type.items(), key=lambda x: -x[1]):
            click.echo(f"    {t}: {count}")
