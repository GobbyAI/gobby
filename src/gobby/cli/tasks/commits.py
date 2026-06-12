"""
CLI commands for commit linking.
"""

from pathlib import Path

import click

from gobby.cli.tasks._utils import get_task_manager, resolve_task_id
from gobby.tasks.commits import auto_link_commits, get_task_diff
from gobby.utils.project_context import get_project_context


@click.group("commit")
def commit_cmd() -> None:
    """Manage commit links for tasks."""
    pass


def _get_project_cwd() -> str | None:
    ctx = get_project_context(cwd=Path.cwd())
    if not ctx:
        return None

    project_path = ctx.get("project_path")
    return str(project_path) if project_path else None


@commit_cmd.command("link")
@click.argument("task_id", metavar="TASK")
@click.argument("commit_sha")
def link_commit(task_id: str, commit_sha: str) -> None:
    """Link a commit to a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    COMMIT_SHA: Git commit SHA (short or full)
    """
    manager = get_task_manager()
    task = resolve_task_id(manager, task_id)
    if not task:
        raise SystemExit(1)

    try:
        cwd = _get_project_cwd()
        updated_task = manager.link_commit(task.id, commit_sha, cwd=cwd)
        stored_sha = updated_task.commits[-1] if updated_task.commits else commit_sha
        click.echo(f"Linked commit {stored_sha} to task {task.id}")
        if updated_task.commits:
            click.echo(f"Total commits: {len(updated_task.commits)}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@commit_cmd.command("unlink")
@click.argument("task_id", metavar="TASK")
@click.argument("commit_sha")
def unlink_commit(task_id: str, commit_sha: str) -> None:
    """Unlink a commit from a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    COMMIT_SHA: Git commit SHA to remove
    """
    manager = get_task_manager()
    task = resolve_task_id(manager, task_id)
    if not task:
        raise SystemExit(1)

    try:
        existing_commits = list(task.commits or [])
        cwd = _get_project_cwd()
        updated_task = manager.unlink_commit(task.id, commit_sha, cwd=cwd)
        remaining_commits = list(updated_task.commits or [])
        if existing_commits == remaining_commits:
            click.echo(f"Commit {commit_sha} not found on task {task.id}; nothing to unlink")
            if remaining_commits:
                click.echo(f"Remaining commits: {len(remaining_commits)}")
            else:
                click.echo("No commits linked")
            return

        click.echo(f"Unlinked commit {commit_sha} from task {task.id}")
        if remaining_commits:
            click.echo(f"Remaining commits: {len(remaining_commits)}")
        else:
            click.echo("No commits linked")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from None


@commit_cmd.command("list")
@click.argument("task_id", metavar="TASK")
def list_commits(task_id: str) -> None:
    """List commits linked to a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    manager = get_task_manager()
    task = resolve_task_id(manager, task_id)
    if not task:
        raise SystemExit(1)

    commits = task.commits or []
    if not commits:
        click.echo(f"No commits linked to task {task.id}")
        return

    click.echo(f"Commits linked to {task.id} ({len(commits)} total):")
    for commit_sha in commits:
        click.echo(f"  {commit_sha}")


@commit_cmd.command("auto")
@click.option("--task", "-t", "task_id", default=None, help="Filter to specific task ID")
@click.option("--since", "-s", default=None, help="Git --since parameter (e.g., '1 week ago')")
def auto_link(task_id: str | None, since: str | None) -> None:
    """Auto-link commits that mention task IDs.

    Scans commit messages for task ID patterns:
    - [gt-xxxxx] - bracket format
    - gt-xxxxx: - colon format
    - Implements/Fixes/Closes gt-xxxxx
    """
    manager = get_task_manager()

    # Get project repo path
    ctx = get_project_context(cwd=Path.cwd())
    cwd = ctx.get("project_path") if ctx else None

    result = auto_link_commits(
        task_manager=manager,
        task_id=task_id,
        since=since,
        cwd=cwd,
    )

    if result.total_linked == 0:
        click.echo("No new commits linked")
        if result.skipped > 0:
            click.echo(f"Skipped {result.skipped} (already linked or task not found)")
        return

    click.echo(f"Linked {result.total_linked} commits:")
    for tid, commits in result.linked_tasks.items():
        for commit_sha in commits:
            click.echo(f"  {commit_sha} -> {tid}")

    if result.skipped > 0:
        click.echo(f"Skipped {result.skipped} (already linked or task not found)")


@click.command("diff")
@click.argument("task_id", metavar="TASK")
@click.option("--uncommitted", "-u", is_flag=True, help="Include uncommitted changes")
@click.option("--stats", is_flag=True, help="Show stats only (no diff content)")
def diff_cmd(task_id: str, uncommitted: bool, stats: bool) -> None:
    """Show diff for all commits linked to a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    manager = get_task_manager()
    task = resolve_task_id(manager, task_id)
    if not task:
        raise SystemExit(1)

    # Get project repo path
    ctx = get_project_context(cwd=Path.cwd())
    cwd = ctx.get("project_path") if ctx else None

    result = get_task_diff(
        task_id=task.id,
        task_manager=manager,
        include_uncommitted=uncommitted,
        cwd=cwd,
    )

    if stats:
        click.echo(f"Task: {task.id}")
        click.echo(f"Commits: {len(result.commits)}")
        click.echo(f"Files modified: {result.file_count}")
        click.echo(f"Has uncommitted changes: {result.has_uncommitted_changes}")
        return

    if not result.diff:
        if not result.commits:
            click.echo(f"No commits linked to task {task.id}")
        else:
            click.echo("No changes in diff (empty diff)")
        return

    # Output the diff
    click.echo(result.diff)
