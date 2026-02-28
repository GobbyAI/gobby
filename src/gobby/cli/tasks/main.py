"""
Task management commands - entry point and misc utilities.
"""

import logging
from typing import Any

import click

from gobby.cli.tasks._utils import (
    check_tasks_enabled,
    get_sync_manager,
    get_task_manager,
)
from gobby.cli.tasks.ai import (
    complexity_cmd,
    suggest_cmd,
    validate_task_cmd,
)
from gobby.cli.tasks.commits import commit_cmd, diff_cmd
from gobby.cli.tasks.crud import (
    blocked_tasks,
    close_task_cmd,
    create_task,
    de_escalate_cmd,
    delete_task,
    list_tasks,
    ready_tasks,
    reopen_task_cmd,
    show_task,
    task_stats,
    update_task,
    validation_history_cmd,
)
from gobby.cli.tasks.deps import dep_cmd
from gobby.cli.tasks.labels import label_cmd
from gobby.cli.tasks.search import reindex_tasks, search_tasks

logger = logging.getLogger(__name__)


@click.group()
def tasks() -> None:
    """Manage development tasks."""
    check_tasks_enabled()


# Register CRUD commands from extracted module
tasks.add_command(list_tasks)
tasks.add_command(ready_tasks)
tasks.add_command(blocked_tasks)
tasks.add_command(task_stats)
tasks.add_command(create_task)
tasks.add_command(show_task)
tasks.add_command(update_task)
tasks.add_command(close_task_cmd)
tasks.add_command(reopen_task_cmd)
tasks.add_command(delete_task)
tasks.add_command(de_escalate_cmd)
tasks.add_command(validation_history_cmd)

# Register AI-powered commands from extracted module
tasks.add_command(validate_task_cmd)
tasks.add_command(complexity_cmd)
tasks.add_command(suggest_cmd)

# Register search commands
tasks.add_command(search_tasks)
tasks.add_command(reindex_tasks)


@tasks.command("sync")
@click.option("--import", "do_import", is_flag=True, help="Import tasks from JSONL")
@click.option("--export", "do_export", is_flag=True, help="Export tasks to JSONL")
@click.option("--quiet", "-q", is_flag=True, help="Suppress output")
def sync_tasks(do_import: bool, do_export: bool, quiet: bool) -> None:
    """Sync tasks with .gobby/tasks.jsonl.

    If neither --import nor --export specified, does both.
    """
    manager = get_sync_manager()

    # Default to both if neither specified
    if not do_import and not do_export:
        do_import = True
        do_export = True

    if do_import:
        if not quiet:
            click.echo("Importing tasks...")
        manager.import_from_jsonl()

    if do_export:
        if not quiet:
            click.echo("Exporting tasks...")
        manager.export_to_jsonl()

    if not quiet:
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


# Register subgroups from extracted modules
tasks.add_command(dep_cmd)
tasks.add_command(label_cmd)
tasks.add_command(commit_cmd)
tasks.add_command(diff_cmd)


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
            click.echo(f"  Task {t['id']}: {t['title']} (Project ID: {t['project_id']})", err=True)
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
