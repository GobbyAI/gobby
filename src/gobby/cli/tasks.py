"""
Task management commands.
"""

import json
import logging
from pathlib import Path
from typing import Any

import click
from gobby.storage.database import LocalDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.sync.tasks import TaskSyncManager
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)


def get_task_manager() -> LocalTaskManager:
    """Get initialized task manager."""
    db = LocalDatabase()
    return LocalTaskManager(db)


def get_sync_manager() -> TaskSyncManager:
    """Get initialized sync manager."""
    manager = get_task_manager()

    # Check for stealth mode in project config
    project_ctx = get_project_context()
    export_path = ".gobby/tasks.jsonl"

    if project_ctx and project_ctx.get("tasks_stealth"):
        # Use home directory storage for stealth mode
        project_id = project_ctx.get("id")
        if project_id:
            home = Path.home()
            stealth_dir = home / ".gobby" / "stealth_tasks"
            stealth_dir.mkdir(parents=True, exist_ok=True)
            export_path = str(stealth_dir / f"{project_id}.jsonl")

    return TaskSyncManager(manager, export_path=export_path)


def format_task_row(task: Task) -> str:
    """Format a task for list output."""
    status_icon = {
        "open": "○",
        "in_progress": "●",
        "completed": "✓",
        "blocked": "⊗",
    }.get(task.status, "?")

    priority_icon = {
        1: "🔴",  # High
        2: "🟡",  # Medium
        3: "🔵",  # Low
    }.get(task.priority, "⚪")

    # Show full ID for usability - users need complete IDs for commands
    return f"{task.id} {status_icon} {priority_icon} {task.title}"


@click.group()
def tasks() -> None:
    """Manage development tasks."""
    pass


@tasks.command("list")
@click.option("--status", help="Filter by status (open, in_progress, completed, blocked)")
@click.option("--assignee", help="Filter by assignee")
@click.option("--ready", is_flag=True, help="Show only ready tasks (open with no blocking deps)")
@click.option("--limit", default=50, help="Max tasks to show")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_tasks(
    status: str | None, assignee: str | None, ready: bool, limit: int, json_format: bool
) -> None:
    """List tasks."""
    project_ctx = get_project_context()
    project_id = project_ctx.get("id") if project_ctx else None

    manager = get_task_manager()

    if ready:
        # Use ready task detection (open tasks with no unresolved blocking dependencies)
        tasks_list = manager.list_ready_tasks(
            project_id=project_id,
            assignee=assignee,
            limit=limit,
        )
    else:
        tasks_list = manager.list_tasks(
            project_id=project_id,
            status=status,
            assignee=assignee,
            limit=limit,
        )

    if json_format:
        click.echo(json.dumps([t.to_dict() for t in tasks_list], indent=2, default=str))
        return

    if not tasks_list:
        click.echo("No tasks found." if not ready else "No ready tasks found.")
        return

    label = "ready tasks" if ready else "tasks"
    click.echo(f"Found {len(tasks_list)} {label}:")
    for task in tasks_list:
        click.echo(format_task_row(task))


@tasks.command("create")
@click.argument("title")
@click.option("--description", "-d", help="Task description")
@click.option("--priority", "-p", type=int, default=2, help="Priority (1=High, 2=Med, 3=Low)")
@click.option("--type", "-t", "task_type", default="task", help="Task type")
def create_task(title: str, description: str | None, priority: int, task_type: str) -> None:
    """Create a new task."""
    project_ctx = get_project_context()
    if not project_ctx or "id" not in project_ctx:
        click.echo("Error: Not in a gobby project or project.json missing 'id'.", err=True)
        return

    manager = get_task_manager()
    task = manager.create_task(
        project_id=project_ctx["id"],
        title=title,
        description=description,
        priority=priority,
        task_type=task_type,
    )
    click.echo(f"Created task {task.id[:8]}: {task.title}")


def resolve_task_id(manager: LocalTaskManager, task_id: str) -> Task | None:
    """Resolve a task ID (exact or prefix match) with user-friendly errors."""
    # Try exact match first
    try:
        return manager.get_task(task_id)
    except ValueError:
        pass

    # Try prefix matching
    matches = manager.find_tasks_by_prefix(task_id)

    if len(matches) == 0:
        click.echo(f"Task '{task_id}' not found", err=True)
        return None
    elif len(matches) == 1:
        return matches[0]
    else:
        click.echo(f"Ambiguous task ID '{task_id}' matches {len(matches)} tasks:", err=True)
        for task in matches[:5]:
            click.echo(f"  {task.id}: {task.title}", err=True)
        if len(matches) > 5:
            click.echo(f"  ... and {len(matches) - 5} more", err=True)
        return None


@tasks.command("show")
@click.argument("task_id")
def show_task(task_id: str) -> None:
    """Show details for a task."""
    manager = get_task_manager()
    task = resolve_task_id(manager, task_id)

    if not task:
        return

    click.echo(f"Task: {task.title}")
    click.echo(f"ID: {task.id}")
    click.echo(f"Status: {task.status}")
    click.echo(f"Priority: {task.priority}")
    click.echo(f"Type: {task.task_type}")
    click.echo(f"Created: {task.created_at}")
    click.echo(f"Updated: {task.updated_at}")
    if task.assignee:
        click.echo(f"Assignee: {task.assignee}")
    if task.labels:
        click.echo(f"Labels: {', '.join(task.labels)}")
    if task.description:
        click.echo(f"\n{task.description}")


@tasks.command("update")
@click.argument("task_id")
@click.option("--title", help="New title")
@click.option("--status", help="New status")
@click.option("--priority", type=int, help="New priority")
@click.option("--assignee", help="New assignee")
def update_task(
    task_id: str, title: str | None, status: str | None, priority: int | None, assignee: str | None
) -> None:
    """Update a task."""
    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    task = manager.update_task(
        resolved.id,
        title=title,
        status=status,
        priority=priority,
        assignee=assignee,
    )
    click.echo(f"Updated task {task.id[:8]}")


@tasks.command("close")
@click.argument("task_id")
@click.option("--reason", default="completed", help="Reason for closing")
def close_task(task_id: str, reason: str) -> None:
    """Close a task."""
    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    task = manager.close_task(resolved.id, reason=reason)
    click.echo(f"Closed task {task.id[:8]} ({reason})")


@tasks.command("delete")
@click.argument("task_id")
@click.option("--cascade", is_flag=True, help="Delete child tasks")
@click.confirmation_option(prompt="Are you sure you want to delete this task?")
def delete_task(task_id: str, cascade: bool) -> None:
    """Delete a task."""
    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    manager.delete_task(resolved.id, cascade=cascade)
    click.echo(f"Deleted task {resolved.id}")


@tasks.command("sync")
@click.option("--direction", default="both", type=click.Choice(["import", "export", "both"]))
def sync_tasks(direction: str) -> None:
    """Sync tasks with .gobby/tasks.jsonl."""
    manager = get_sync_manager()

    if direction in ["import", "both"]:
        click.echo("Importing tasks...")
        manager.import_from_jsonl()

    if direction in ["export", "both"]:
        click.echo("Exporting tasks...")
        manager.export_to_jsonl()

    click.echo("Sync completed")


@tasks.group("compact")
def compact_cmd() -> None:
    """Task compaction commands."""
    pass


@compact_cmd.command("analyze")
@click.option("--days", type=int, default=30, help="Days blocked threshold")
def compact_analyze(days: int) -> None:
    """Find tasks eligible for compaction."""
    manager = get_task_manager()
    from gobby.storage.compaction import TaskCompactor

    compactor = TaskCompactor(manager)
    candidates = compactor.find_candidates(days_closed=days)

    if not candidates:
        click.echo("No compaction candidates found.")
        return

    click.echo(f"Found {len(candidates)} candidates closed > {days} days:")
    for task in candidates:
        click.echo(f"  {task['id']}: {task['title']} (Updated: {task['updated_at']})")


@compact_cmd.command("apply")
@click.option("--id", "task_id", required=True, help="Task ID to compact")
@click.option("--summary", required=True, help="Summary text or file path (@path)")
def compact_apply(task_id: str, summary: str) -> None:
    """Compact a task with a summary."""
    manager = get_task_manager()
    from gobby.storage.compaction import TaskCompactor

    # Handle file input for summary
    if summary.startswith("@"):
        path = summary[1:]
        try:
            with open(path) as f:
                summary_content = f.read()
        except Exception as e:
            click.echo(f"Error reading summary file: {e}", err=True)
            return
    else:
        summary_content = summary

    compactor = TaskCompactor(manager)
    try:
        compactor.compact_task(task_id, summary_content)
        click.echo(f"Compacted task {task_id}.")
    except Exception as e:
        click.echo(f"Error compacting task: {e}", err=True)


@compact_cmd.command("stats")
def compact_stats() -> None:
    """Show compaction statistics."""
    manager = get_task_manager()
    from gobby.storage.compaction import TaskCompactor

    compactor = TaskCompactor(manager)
    stats = compactor.get_stats()

    click.echo("Compaction Statistics:")
    click.echo(f"  Total Closed: {stats['total_closed']}")
    click.echo(f"  Compacted:    {stats['compacted']}")
    click.echo(f"  Rate:         {stats['rate']}%")


@tasks.group("label")
def label_cmd() -> None:
    """Manage task labels."""
    pass


@label_cmd.command("add")
@click.argument("task_id")
@click.argument("label")
def add_label(task_id: str, label: str) -> None:
    """Add a label to a task."""
    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    manager.add_label(resolved.id, label)
    click.echo(f"Added label '{label}' to task {resolved.id}")


@label_cmd.command("remove")
@click.argument("task_id")
@click.argument("label")
def remove_label(task_id: str, label: str) -> None:
    """Remove a label from a task."""
    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    manager.remove_label(resolved.id, label)
    click.echo(f"Removed label '{label}' from task {resolved.id}")


@tasks.group("import")
def import_cmd() -> None:
    """Import tasks from external sources."""
    pass


@import_cmd.command("github")
@click.argument("url")
@click.option("--limit", default=50, help="Max issues to import")
def import_github(url: str, limit: int) -> None:
    """Import open issues from GitHub."""
    import asyncio

    manager = get_sync_manager()

    # We need to run async method
    async def run() -> dict[str, Any]:
        result: dict[str, Any] = await manager.import_from_github_issues(url, limit=limit)
        return result

    try:
        result = asyncio.run(run())

        if result["success"]:
            click.echo(result["message"])
            for issue_id in result["imported"]:
                click.echo(f"  Imported {issue_id}")
        else:
            click.echo(f"Error: {result['error']}", err=True)
    except Exception as e:
        click.echo(f"Failed to run import: {e}", err=True)


@tasks.command("stealth")
@click.option("--enable/--disable", default=None, help="Enable or disable stealth mode")
def stealth_cmd(enable: bool | None) -> None:
    """
    Manage stealth mode (store tasks outside repo).

    When enabled, tasks are stored in ~/.gobby/stealth_tasks/ instead of .gobby/tasks.jsonl.
    This prevents task updates from creating Git changes.
    """
    import json
    from pathlib import Path

    ctx = get_project_context()
    if not ctx or "project_path" not in ctx:
        click.echo("Error: Not in a gobby project.", err=True)
        return

    project_root = Path(ctx["project_path"])
    config_path = project_root / ".gobby" / "project.json"

    if not config_path.exists():
        click.echo("Error: project.json not found.", err=True)
        return

    # Read current config
    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception as e:
        click.echo(f"Error reading project config: {e}", err=True)
        return

    current_state = config.get("tasks_stealth", False)

    if enable is None:
        # Show status
        status = "ENABLED" if current_state else "DISABLED"
        click.echo(f"Stealth mode is currently {status}")
        if current_state:
            click.echo(f"Tasks stored in ~/.gobby/stealth_tasks/{ctx.get('id')}.jsonl")
        else:
            click.echo("Tasks stored in .gobby/tasks.jsonl")
        return

    if enable == current_state:
        click.echo(f"Stealth mode is already {'enabled' if enable else 'disabled'}.")
        return

    # Update config
    config["tasks_stealth"] = enable
    try:
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
            # Add newline at end of file
            f.write("\n")

        click.echo(f"Stealth mode {'enabled' if enable else 'disabled'}.")
        if enable:
            click.echo("Future tasks will be synced to ~/.gobby/stealth_tasks/")
            click.echo(
                "Note: Existing tasks in .gobby/tasks.jsonl remain there but won't be updated."
            )
        else:
            click.echo("Future tasks will be synced to .gobby/tasks.jsonl")

    except Exception as e:
        click.echo(f"Failed to update config: {e}", err=True)


@tasks.command("doctor")
def doctor_cmd() -> None:
    """Validate task data integrity."""
    manager = get_task_manager()
    from gobby.utils.validation import TaskValidator

    validator = TaskValidator(manager)
    results = validator.validate_all()

    issues_found = False

    orphans = results["orphan_dependencies"]
    if orphans:
        issues_found = True
        click.echo(f"Found {len(orphans)} orphan dependencies:", err=True)
        for d in orphans:
            click.echo(f"  Dependency {d['id']}: {d['task_id']} -> {d['depends_on']}", err=True)
    else:
        click.echo("✓ No orphan dependencies")

    invalid_projects = results["invalid_projects"]
    if invalid_projects:
        issues_found = True
        click.echo(f"Found {len(invalid_projects)} tasks with invalid projects:", err=True)
        for t in invalid_projects:
            click.echo(f"  Task {t['id']}: {t['title']} (Project: {t['project_id']})", err=True)
    else:
        click.echo("✓ No invalid projects")

    cycles = results["cycles"]
    if cycles:
        issues_found = True
        click.echo(f"Found {len(cycles)} dependency cycles:", err=True)
        for cycle in cycles:
            click.echo(f"  Cycle: {' -> '.join(cycle)}", err=True)
    else:
        click.echo("✓ No dependency cycles")

    if issues_found:
        click.echo("\nIssues found. Run 'gobby tasks clean' to fix fixable issues.")
        # Exit with error code if issues found
        # (Click handles exit code but we can explicitly exit if needed, usually just return is fine unless we want non-zero)


@tasks.command("clean")
@click.confirmation_option(prompt="This will remove orphaned dependencies. Are you sure?")
def clean_cmd() -> None:
    """Fix data integrity issues (remove orphans)."""
    manager = get_task_manager()
    from gobby.utils.validation import TaskValidator

    validator = TaskValidator(manager)
    count = validator.clean_orphans()

    if count > 0:
        click.echo(f"Removed {count} orphan dependencies.")
    else:
        click.echo("No orphan dependencies found.")
