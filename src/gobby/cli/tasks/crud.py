"""
CRUD commands for task management.
"""

import json
from pathlib import Path
from typing import Any

import click

from gobby.cli.tasks._stage_filters import STAGE_STATE_CHOICE, filter_tasks_by_stage
from gobby.cli.tasks._utils import (
    collect_ancestors,
    compute_tree_prefixes,
    format_task_list,
    get_claimed_task_ids,
    get_task_manager,
    resolve_task_id,
    sort_tasks_for_tree,
)
from gobby.cli.utils import resolve_project_ref
from gobby.storage.tasks import TASK_TYPE_CHOICES
from gobby.tasks.isolation import validate_task_isolation_artifacts
from gobby.tasks.state_semantics import is_task_closed, serialize_task_state
from gobby.utils.project_context import get_project_context

TASK_TYPE_CHOICE = click.Choice(TASK_TYPE_CHOICES)
ISOLATION_CHOICE = click.Choice(["none", "worktree", "clone"])


def _current_stage_display(state: dict[str, Any]) -> str:
    current_stage = state.get("current_stage")
    if isinstance(current_stage, dict):
        name = current_stage.get("name")
        stage_state = current_stage.get("state")
        if name and stage_state:
            return f"{name}:{stage_state}"
        if stage_state:
            return str(stage_state)
    return "ready"


@click.command("list")
@click.option(
    "--active",
    is_flag=True,
    help="Show all non-closed work",
)
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option("--stage", "stage_name", help="Filter by exact stage name")
@click.option("--state", "stage_state", type=STAGE_STATE_CHOICE, help="Filter by stage state")
@click.option("--assignee", "-a", help="Filter by assignee")
@click.option("--claimed", is_flag=True, help="Show only claimed tasks")
@click.option("--unclaimed", is_flag=True, help="Show only unclaimed tasks")
@click.option(
    "--ready",
    is_flag=True,
    help="Show only ready tasks with no unresolved blocking dependencies",
)
@click.option("--blocked", is_flag=True, help="Show only canonically blocked tasks")
@click.option("--closed", "closed_only", is_flag=True, help="Show only canonically closed tasks")
@click.option("--escalated", is_flag=True, help="Show only canonically escalated tasks")
@click.option("--limit", "-l", default=50, help="Max tasks to show")
@click.option(
    "--group",
    "group_by",
    type=click.Choice(["project", "stage"]),
    default=None,
    help=(
        "Group output by project or stage. When omitted, tasks are "
        "auto-grouped by project if no project context is detected."
    ),
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_tasks(
    active: bool,
    project_ref: str | None,
    stage_name: str | None,
    stage_state: str | None,
    assignee: str | None,
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
    """List tasks."""
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

    project_id = resolve_project_ref(project_ref)

    manager = get_task_manager()

    if ready:
        # Use ready task detection (open/in_progress tasks with no unresolved blocking dependencies)
        tasks_list = manager.list_ready_tasks(
            project_id=project_id,
            assignee=assignee,
            limit=limit,
        )
        label = "ready tasks"
        empty_msg = "No ready tasks found."
    elif blocked:
        # Show tasks that are blocked by unresolved dependencies
        tasks_list = manager.list_blocked_tasks(
            project_id=project_id,
            limit=limit,
        )
        label = "blocked tasks"
        empty_msg = "No blocked tasks found."
    else:
        tasks_list = manager.list_tasks(
            project_id=project_id,
            assignee=assignee,
            claimed=claimed_filter,
            closed=closed_filter,
            limit=10000 if stage_name or escalated else limit,
        )
        if escalated:
            tasks_list = [
                task for task in tasks_list if serialize_task_state(task)["is_escalated"]
            ][:limit]
        tasks_list = filter_tasks_by_stage(
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
        click.echo(json.dumps([t.to_dict() for t in tasks_list], indent=2, default=str))
        return

    if not tasks_list:
        click.echo(empty_msg)
        return

    # For filtered views, include ancestors for proper tree hierarchy
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
        display_tasks, primary_ids = collect_ancestors(tasks_list, manager)

    # Sort for proper tree display order
    display_tasks = sort_tasks_for_tree(display_tasks)

    # Get tasks claimed by active sessions for indicator display
    claimed_ids = get_claimed_task_ids()

    # Auto-group by project when running outside a project context unless the
    # user already asked for a specific grouping.
    effective_group_by = group_by
    if effective_group_by is None and project_id is None:
        effective_group_by = "project"

    # Tree prefixes are precomputed against the global display order. When
    # grouping by stage, children can land in a different bucket than their
    # parent, leaving glyphs pointing at rows that aren't adjacent.
    prefixes = (
        compute_tree_prefixes(display_tasks, primary_ids) if effective_group_by != "stage" else None
    )
    click.echo(f"Found {len(tasks_list)} {label}:")
    rendered = format_task_list(
        display_tasks,
        claimed_task_ids=claimed_ids,
        primary_ids=primary_ids,
        tree_prefixes=prefixes,
        group_by=effective_group_by,
        db=manager.db,
    )
    if rendered:
        click.echo(rendered)


@click.command("ready")
@click.option("--limit", "-n", default=10, help="Max results")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option("--priority", type=int, help="Filter by priority")
@click.option("--type", "-t", "task_type", help="Filter by type")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.option("--flat", is_flag=True, help="Flat list without tree hierarchy")
def ready_tasks(
    limit: int,
    project_ref: str | None,
    priority: int | None,
    task_type: str | None,
    json_format: bool,
    flat: bool,
) -> None:
    """List tasks with no unresolved blocking dependencies."""
    project_id = resolve_project_ref(project_ref)
    manager = get_task_manager()
    tasks_list = manager.list_ready_tasks(
        project_id=project_id,
        priority=priority,
        task_type=task_type,
        limit=limit,
    )

    if json_format:
        click.echo(json.dumps([t.to_dict() for t in tasks_list], indent=2, default=str))
        return

    if not tasks_list:
        click.echo("No ready tasks found.")
        return

    # Get tasks claimed by active sessions for indicator display
    claimed_ids = get_claimed_task_ids()

    click.echo(f"Found {len(tasks_list)} ready tasks:")

    if flat:
        rendered = format_task_list(
            list(tasks_list),
            claimed_task_ids=claimed_ids,
            db=manager.db,
        )
    else:
        display_tasks, primary_ids = collect_ancestors(tasks_list, manager)
        display_tasks = sort_tasks_for_tree(display_tasks)
        prefixes = compute_tree_prefixes(display_tasks, primary_ids)
        rendered = format_task_list(
            display_tasks,
            claimed_task_ids=claimed_ids,
            primary_ids=primary_ids,
            tree_prefixes=prefixes,
            db=manager.db,
        )
    if rendered:
        click.echo(rendered)


@click.command("blocked")
@click.option("--limit", "-n", default=20, help="Max results")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def blocked_tasks(limit: int, project_ref: str | None, json_format: bool) -> None:
    """List blocked tasks with what blocks them."""
    from gobby.storage.task_dependencies import TaskDependencyManager

    project_id = resolve_project_ref(project_ref)
    manager = get_task_manager()
    dep_manager = TaskDependencyManager(manager.db)
    blocked_list = manager.list_blocked_tasks(project_id=project_id, limit=limit)

    if json_format:
        # Build detailed structure for JSON output
        result = []
        for task in blocked_list:
            tree = dep_manager.get_dependency_tree(task.id)
            result.append(
                {
                    "task": task.to_dict(),
                    "blocked_by": tree.get("blockers", []),
                }
            )
        click.echo(json.dumps(result, indent=2, default=str))
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

                # Explicit cast to satisfy linter
                bid: str = blocker_id

                try:
                    blocker_task = manager.get_task(bid)
                    status_icon = "✓" if is_task_closed(blocker_task) else "○"
                    click.echo(f"    {status_icon} {bid[:8]}: {blocker_task.title}")
                except Exception:
                    click.echo(f"    ? {bid[:8]}: (not found)")


@click.command("stats")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def task_stats(project_ref: str | None, json_format: bool) -> None:
    """Show task statistics."""
    project_id = resolve_project_ref(project_ref)
    manager = get_task_manager()

    all_tasks = manager.list_tasks(project_id=project_id, limit=10000)
    total = len(all_tasks)
    by_stage_state: dict[str, int] = {}
    by_priority = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
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
            stage_display = _current_stage_display(state)
            by_stage_state[stage_display] = by_stage_state.get(stage_display, 0) + 1
            if state["is_claimed"]:
                claimed_count += 1
            else:
                unclaimed_count += 1
            if state["is_escalated"]:
                escalated_count += 1
            if state["is_merge_ready"]:
                merge_ready_count += 1

        if task.priority is not None:
            by_priority[task.priority] = by_priority.get(task.priority, 0) + 1
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
        click.echo(json.dumps(stats, indent=2))
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
    if by_type:
        click.echo("\n  By Type:")
        for t, count in sorted(by_type.items(), key=lambda x: -x[1]):
            click.echo(f"    {t}: {count}")


@click.command("create")
@click.argument("title")
@click.option("--description", "-d", help="Task description")
@click.option("--priority", "-p", type=int, default=2, help="Priority (1=High, 2=Med, 3=Low)")
@click.option("--type", "-t", "task_type", type=TASK_TYPE_CHOICE, default="task", help="Task type")
@click.option("--depends-on", "-D", multiple=True, help="Task(s) this task depends on (#N, UUID)")
@click.option("--project", "project_ref", help="Target project (name or UUID)")
def create_task(
    title: str,
    description: str | None,
    priority: int,
    task_type: str,
    depends_on: tuple[str, ...],
    project_ref: str | None,
) -> None:
    """Create a new task.

    Examples:
        gobby tasks create "Fix bug"
        gobby tasks create "Implement feature" --depends-on "#1"
        gobby tasks create "Final review" -D "#1" -D "#2"
        gobby tasks create "Note" --project _personal
    """
    from gobby.storage.projects import PERSONAL_PROJECT_ID

    project_id = resolve_project_ref(project_ref)
    if not project_id:
        # No explicit project and no project context — use personal workspace
        project_id = PERSONAL_PROJECT_ID

    manager = get_task_manager()
    task = manager.create_task(
        project_id=project_id,
        title=title,
        description=description,
        priority=priority,
        task_type=task_type,
    )
    task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]
    project_ctx = get_project_context(cwd=Path.cwd())
    project_name = project_ctx.get("name") if project_ctx else None

    if project_name and task.seq_num:
        click.echo(f"Created task {project_name}-#{task.seq_num}: {task.title}")
    else:
        click.echo(f"Created task {task_ref}: {task.title}")

    # Handle depends_on
    if depends_on:
        from gobby.storage.task_dependencies import TaskDependencyManager

        dep_manager = TaskDependencyManager(manager.db)
        for blocker_ref in depends_on:
            try:
                blocker = resolve_task_id(manager, blocker_ref)
                if blocker:
                    # blocker blocks task (task depends on blocker)
                    dep_manager.add_dependency(task.id, blocker.id, "blocks")
                    blocker_display = f"#{blocker.seq_num}" if blocker.seq_num else blocker.id[:8]
                    click.echo(f"  → depends on {blocker_display}")
            except Exception as e:
                click.echo(f"  Warning: Could not add dependency on '{blocker_ref}': {e}", err=True)


@click.command("show")
@click.argument("task_id", metavar="TASK")
def show_task(task_id: str) -> None:
    """Show details for a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    manager = get_task_manager()
    task = resolve_task_id(manager, task_id)

    if not task:
        return

    blocker_ids = sorted(task.active_blocked_by)
    state = serialize_task_state(task)
    if state["is_closed"]:
        stage_display = "closed"
    elif state["is_escalated"]:
        stage_display = "escalated"
    else:
        stage_display = _current_stage_display(state)

    click.echo(f"Task: {task.title}")
    click.echo(f"ID: {task.id}")
    if task.seq_num:
        click.echo(f"Ref: #{task.seq_num}")
    click.echo(f"Current Stage: {stage_display}")
    click.echo(f"Owner Session: {state['owner_session_id'] or '-'}")
    click.echo(f"Blocked: {'yes' if state['is_blocked'] else 'no'}")
    click.echo(f"Escalated: {'yes' if state['is_escalated'] else 'no'}")
    click.echo(f"Merge Ready: {'yes' if state['is_merge_ready'] else 'no'}")
    click.echo(f"Closed: {'yes' if state['is_closed'] else 'no'}")
    click.echo(f"Priority: {task.priority}")
    click.echo(f"Type: {task.task_type}")
    click.echo(f"Created: {task.created_at}")
    click.echo(f"Updated: {task.updated_at}")
    if state["closed_at"]:
        click.echo(f"Closed At: {state['closed_at']}")
    if state["closed_reason"]:
        click.echo(f"Closed Reason: {state['closed_reason']}")
    if state["closed_in_session_id"]:
        click.echo(f"Closed In Session: {state['closed_in_session_id']}")
    if state["closed_commit_sha"]:
        click.echo(f"Closed Commit: {state['closed_commit_sha']}")
    if state["escalated_at"]:
        click.echo(f"Escalated At: {state['escalated_at']}")
    if state["escalation_reason"]:
        click.echo(f"Escalation Reason: {state['escalation_reason']}")
    if task.assignee:
        click.echo(f"Compatibility Assignee: {task.assignee}")
    if task.labels:
        click.echo(f"Labels: {', '.join(task.labels)}")
    if blocker_ids:
        blocker_refs: list[str] = []
        for blocker_id in blocker_ids:
            try:
                blocker_task = manager.get_task(blocker_id)
                blocker_refs.append(
                    f"#{blocker_task.seq_num}" if blocker_task.seq_num else blocker_task.id[:8]
                )
            except Exception:
                blocker_refs.append(blocker_id[:8])
        click.echo(f"Blocked By: {', '.join(blocker_refs)}")
    if task.description:
        click.echo(f"\n{task.description}")


@click.command("update")
@click.argument("task_id", metavar="TASK")
@click.option("--title", "-T", help="New title")
@click.option("--priority", type=int, help="New priority")
@click.option("--parent", "parent_task_id", help="Parent task (#N, path, or UUID)")
@click.option(
    "--task-type",
    "task_type",
    type=TASK_TYPE_CHOICE,
    help="New task type",
)
@click.option(
    "--isolation",
    type=ISOLATION_CHOICE,
    help="New automation isolation mode",
)
def update_task(
    task_id: str,
    title: str | None,
    priority: int | None,
    parent_task_id: str | None,
    task_type: str | None,
    isolation: str | None,
) -> None:
    """Update a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    # Resolve parent task ID if provided
    resolved_parent_id = None
    if parent_task_id:
        resolved_parent = resolve_task_id(manager, parent_task_id)
        if not resolved_parent:
            return
        resolved_parent_id = resolved_parent.id

    # Only pass parameters that were explicitly provided (not None)
    # to avoid setting NOT NULL fields to NULL
    kwargs: dict[str, Any] = {}
    if title is not None:
        kwargs["title"] = title
    if priority is not None:
        kwargs["priority"] = priority
    if resolved_parent_id is not None:
        kwargs["parent_task_id"] = resolved_parent_id
    if task_type is not None:
        kwargs["task_type"] = task_type

    try:
        if isolation is not None:
            kwargs["isolation"] = validate_task_isolation_artifacts(manager, resolved.id, isolation)
        task = manager.update_task(resolved.id, **kwargs)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    # Use standardized ref
    task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]
    click.echo(f"Updated task {task_ref}")


@click.command("close")
@click.argument("task_ids", metavar="TASK", nargs=-1, required=True)
@click.option("--reason", "-r", default="completed", help="Reason for closing")
@click.option("--skip-validation", is_flag=True, help="Skip validation checks")
@click.option("--force", "-f", is_flag=True, help="Alias for --skip-validation")
def close_task_cmd(
    task_ids: tuple[str, ...], reason: str, skip_validation: bool, force: bool
) -> None:
    """Close one or more tasks.

    TASK can be: #N (e.g., #1, #47), seq_num (e.g., 47), path (e.g., 1.2.3), or UUID.
    Multiple tasks can be specified separated by spaces or commas.

    Examples:
        gobby tasks close #42
        gobby tasks close 42 43 44
        gobby tasks close abc123,#45,46

    Parent tasks require all children to be closed first.
    Use --skip-validation or --force for wont_fix, duplicate, etc.
    """
    manager = get_task_manager()
    skip = skip_validation or force

    # Expand comma-separated values into individual IDs
    expanded_ids: list[str] = []
    for task_id in task_ids:
        if "," in task_id:
            expanded_ids.extend(part.strip() for part in task_id.split(",") if part.strip())
        else:
            expanded_ids.append(task_id)

    closed_count = 0
    failed_count = 0

    for task_id in expanded_ids:
        resolved = resolve_task_id(manager, task_id)
        if not resolved:
            failed_count += 1
            continue

        if not skip:
            # Check if task has children (is a parent task)
            children = manager.list_tasks(parent_task_id=resolved.id, limit=1000)

            if children:
                # Parent task: must have all children closed
                open_children = [c for c in children if not is_task_closed(c)]
                if open_children:
                    task_ref = f"#{resolved.seq_num}" if resolved.seq_num else resolved.id[:8]
                    click.echo(
                        f"Cannot close {task_ref}: {len(open_children)} child tasks still open",
                        err=True,
                    )
                    failed_count += 1
                    continue

        task = manager.close_task(resolved.id, reason=reason)

        # Use standardized ref
        task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]
        click.echo(f"Closed task {task_ref} ({reason})")
        closed_count += 1

    # Summary if multiple tasks were processed
    if len(expanded_ids) > 1:
        if failed_count > 0:
            click.echo(f"\nClosed {closed_count}/{len(expanded_ids)} tasks ({failed_count} failed)")
        else:
            click.echo(f"\nClosed {closed_count} tasks")


@click.command("reopen")
@click.argument("task_id", metavar="TASK")
@click.option("--reason", "-r", default=None, help="Reason for reopening")
def reopen_task_cmd(task_id: str, reason: str | None) -> None:
    """Reopen a task to active stage state.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.

    Works on closed or escalated tasks. Clears ownership, closure/escalation
    fields, and resets validation_fail_count.
    """
    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    # Use standardized ref for errors
    resolved_ref = f"#{resolved.seq_num}" if resolved.seq_num else resolved.id[:8]

    if not is_task_closed(resolved) and not resolved.is_escalated:
        click.echo(
            f"Task {resolved_ref} is already active",
            err=True,
        )
        return

    task = manager.reopen_task(resolved.id, reason=reason)

    # Use standardized ref
    task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]

    if reason:
        click.echo(f"Reopened task {task_ref} ({reason})")
    else:
        click.echo(f"Reopened task {task_ref}")


@click.command("delete")
@click.argument("task_refs", nargs=-1, required=True, metavar="TASKS...")
@click.option("--cascade", "-c", is_flag=True, help="Delete child tasks and dependent tasks")
@click.option(
    "--unlink", "-u", is_flag=True, help="Remove dependency links but preserve dependent tasks"
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def delete_task(task_refs: tuple[str, ...], cascade: bool, unlink: bool, yes: bool) -> None:
    """Delete one or more tasks.

    TASKS can be: #N (e.g., #1, #47), comma-separated (#1,#2,#3), or UUIDs.
    Multiple tasks can be specified separated by spaces or commas.

    Examples:
        gobby tasks delete #42
        gobby tasks delete #42,#43,#44 --cascade
        gobby tasks delete #42 #43 #44 --yes
        gobby tasks delete #42 --unlink
    """
    from gobby.cli.tasks._utils import parse_task_refs

    manager = get_task_manager()

    # Parse and resolve all task refs
    all_refs = parse_task_refs(task_refs)
    resolved_tasks = []
    for ref in all_refs:
        resolved = resolve_task_id(manager, ref)
        if resolved:
            resolved_tasks.append((ref, resolved))

    if not resolved_tasks:
        return

    # Confirm deletion
    if not yes:
        task_list = ", ".join(ref for ref, _ in resolved_tasks)
        if not click.confirm(f"Delete {len(resolved_tasks)} task(s): {task_list}?"):
            click.echo("Cancelled.")
            return

    # Delete tasks
    deleted = 0
    for ref, resolved in resolved_tasks:
        try:
            manager.delete_task(resolved.id, cascade=cascade, unlink=unlink)
            click.echo(f"Deleted task {resolved.id}")
            deleted += 1
        except ValueError as e:
            msg = str(e)
            if "has children" in msg:
                msg = f"Task {ref} has children. Use --cascade to delete with all subtasks."
            elif "dependent task(s)" in msg:
                msg = (
                    f"Task {ref} has dependent tasks. "
                    f"Use --cascade to delete them, or --unlink to preserve them."
                )
            click.echo(f"Error: {msg}", err=True)

    if len(resolved_tasks) > 1:
        click.echo(f"\nDeleted {deleted}/{len(resolved_tasks)} tasks.")


@click.command("de-escalate")
@click.argument("task_id", metavar="TASK")
@click.option("--reason", "-r", required=True, help="Reason for de-escalation")
@click.option("--reset-validation", is_flag=True, help="Reset validation fail count")
@click.option("--reset-stage-attempts", is_flag=True, help="Reset current stage work attempts")
def de_escalate_cmd(
    task_id: str,
    reason: str,
    reset_validation: bool,
    reset_stage_attempts: bool,
) -> None:
    """Return an escalated task to its preserved current stage.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.

    Use after human intervention resolves the issue that caused escalation.
    """
    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    if not resolved.is_escalated:
        click.echo(
            f"Task {resolved.id[:8]} is not escalated",
            err=True,
        )
        return

    manager.de_escalate_task(
        resolved.id,
        reason=reason,
        reset_validation=reset_validation,
        reset_stage_attempts=reset_stage_attempts,
    )
    click.echo(f"De-escalated task {resolved.id[:8]} ({reason})")
    if reset_validation:
        click.echo("  Validation fail count reset to 0")
    if reset_stage_attempts:
        click.echo("  Current stage work attempts reset to 0")


@click.command("validation-history")
@click.argument("task_id", metavar="TASK")
@click.option("--clear", is_flag=True, help="Clear validation history")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def validation_history_cmd(task_id: str, clear: bool, json_format: bool) -> None:
    """View or clear validation history for a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    from gobby.tasks.validation_history import ValidationHistoryManager

    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    history_manager = ValidationHistoryManager(manager.db)

    if clear:
        history_manager.clear_history(resolved.id)
        manager.update_task(resolved.id, validation_fail_count=0)
        click.echo(f"Cleared validation history for {resolved.id[:8]}")
        return

    iterations = history_manager.get_iteration_history(resolved.id)

    if json_format:
        result = {
            "task_id": resolved.id,
            "iterations": [
                {
                    "iteration": it.iteration,
                    "status": it.status,
                    "feedback": it.feedback,
                    "issues": [i.to_dict() for i in (it.issues or [])],
                    "created_at": it.created_at,
                }
                for it in iterations
            ],
        }
        click.echo(json.dumps(result, indent=2, default=str))
        return

    if not iterations:
        click.echo(f"No validation history for task {resolved.id[:8]}")
        return

    click.echo(f"Validation history for {resolved.id[:8]}:")
    for it in iterations:
        click.echo(f"\n  Iteration {it.iteration}: {it.status}")
        if it.feedback:
            feedback_preview = it.feedback[:100] + "..." if len(it.feedback) > 100 else it.feedback
            click.echo(f"    Feedback: {feedback_preview}")
        if it.issues:
            click.echo(f"    Issues: {len(it.issues)}")
        click.echo(f"    Created: {it.created_at}")
