"""
Worktree management CLI commands.

Commands for managing git worktrees:
- create: Create a new worktree
- list: List worktrees
- show: Show worktree details
- delete: Delete a worktree
- spawn: Spawn an agent in a worktree
- claim: Claim a worktree for a session
- release: Release a worktree
- sync: Sync worktree with main branch
- stale: Detect stale worktrees
- cleanup: Clean up stale worktrees
"""

import json

import click
import httpx

from gobby.cli.tasks._utils import get_task_manager, resolve_task_id
from gobby.cli.utils import resolve_project_ref, resolve_session_id
from gobby.storage.database import LocalDatabase
from gobby.storage.worktrees import LocalWorktreeManager


def get_worktree_manager() -> LocalWorktreeManager:
    """Get initialized worktree manager."""
    db = LocalDatabase()
    return LocalWorktreeManager(db)


def get_daemon_url() -> str:
    """Get daemon URL from config."""
    from gobby.config.app import load_config

    config = load_config()
    return f"http://localhost:{config.daemon_port}"


@click.group()
def worktrees() -> None:
    """Manage git worktrees for parallel development."""
    pass


@worktrees.command("create")
@click.argument("branch_name")
@click.option("--base", "-b", "base_branch", default="main", help="Base branch to create from")
@click.option("--task", "-t", "task_id", help="Link worktree to a task")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def create_worktree(
    branch_name: str,
    base_branch: str,
    task_id: str | None,
    json_format: bool,
) -> None:
    """Create a new worktree for parallel development.

    Examples:

        gobby worktrees create feature/my-feature

        gobby worktrees create bugfix/fix-123 --base develop --task #47
    """
    import os

    daemon_url = get_daemon_url()

    arguments = {
        "branch_name": branch_name,
        "base_branch": base_branch,
        "project_path": os.getcwd(),
    }

    if task_id:
        task_manager = get_task_manager()
        resolved = resolve_task_id(task_manager, task_id)
        if not resolved:
            # resolve_task_id prints error
            return
        arguments["task_id"] = resolved.id

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-worktrees/tools/create_worktree",
            json=arguments,
            timeout=60.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        return
    except httpx.HTTPStatusError as e:
        click.echo(f"HTTP Error {e.response.status_code}: {e.response.text}", err=True)
        raise SystemExit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return

    if json_format:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    if result.get("success"):
        click.echo(f"Created worktree: {result.get('worktree_id', 'unknown')}")
        click.echo(f"  Path: {result.get('worktree_path', 'unknown')}")
        click.echo(f"  Branch: {result.get('branch_name', 'unknown')}")
    else:
        click.echo(f"Failed to create worktree: {result.get('error')}", err=True)


@worktrees.command("list")
@click.option("--status", "-s", help="Filter by status (active, stale, merged, abandoned)")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_worktrees(
    status: str | None,
    project_ref: str | None,
    json_format: bool,
) -> None:
    """List worktrees."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    manager = get_worktree_manager()

    worktrees_list = manager.list_worktrees(status=status, project_id=project_id)

    if json_format:
        click.echo(json.dumps([w.to_dict() for w in worktrees_list], indent=2, default=str))
        return

    if not worktrees_list:
        click.echo("No worktrees found.")
        return

    click.echo(f"Found {len(worktrees_list)} worktree(s):\n")
    for wt in worktrees_list:
        status_icon = {
            "active": "●",
            "stale": "○",
            "merged": "✓",
            "abandoned": "✗",
        }.get(wt.status, "?")

        session_info = f" (session: {wt.agent_session_id[:8]})" if wt.agent_session_id else ""
        click.echo(f"{status_icon} {wt.id}  {wt.branch_name:<30} {wt.status:<10}{session_info}")


@worktrees.command("show")
@click.argument("worktree_ref")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def show_worktree(worktree_ref: str, json_format: bool) -> None:
    """Show details for a worktree (UUID or prefix)."""
    manager = get_worktree_manager()
    worktree_id = resolve_worktree_id(manager, worktree_ref)
    worktree = manager.get(worktree_id)

    if not worktree:
        click.echo(f"Worktree not found: {worktree_id}", err=True)
        return

    if json_format:
        click.echo(json.dumps(worktree.to_dict(), indent=2, default=str))
        return

    click.echo(f"Worktree: {worktree.id}")
    click.echo(f"  Status: {worktree.status}")
    click.echo(f"  Branch: {worktree.branch_name}")
    click.echo(f"  Path: {worktree.worktree_path}")
    click.echo(f"  Base Branch: {worktree.base_branch}")
    if worktree.project_id:
        click.echo(f"  Project: {worktree.project_id}")
    if worktree.agent_session_id:
        click.echo(f"  Session: {worktree.agent_session_id}")
    click.echo(f"  Created: {worktree.created_at}")
    click.echo(f"  Updated: {worktree.updated_at}")


@worktrees.command("delete")
@click.argument("worktree_ref")
@click.option("--force", "-f", is_flag=True, help="Force delete even if active")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def delete_worktree(worktree_ref: str, force: bool, yes: bool) -> None:
    """Delete a worktree (UUID or prefix)."""
    if not yes:
        click.confirm("Are you sure you want to delete this worktree?", abort=True)

    manager = get_worktree_manager()
    try:
        worktree_id = resolve_worktree_id(manager, worktree_ref)
    except click.ClickException as e:
        click.echo(str(e), err=True)
        return

    daemon_url = get_daemon_url()

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-worktrees/tools/delete_worktree",
            json={"worktree_id": worktree_id, "force": force},
            timeout=30.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        return
    except httpx.HTTPStatusError as e:
        click.echo(f"HTTP Error {e.response.status_code}: {e.response.text}", err=True)
        return
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return

    if result.get("success"):
        click.echo(f"Deleted worktree: {worktree_id}")
    else:
        click.echo(f"Failed to delete worktree: {result.get('error')}", err=True)


# ... spawn command is unchanged ...


@worktrees.command("claim")
@click.argument("worktree_ref")
@click.argument("session_id")
def claim_worktree(worktree_ref: str, session_id: str) -> None:
    """Claim a worktree for a session (UUID or prefix)."""
    try:
        session_id = resolve_session_id(session_id)
    except click.ClickException as e:
        e.show()
        raise SystemExit(1) from e

    manager = get_worktree_manager()
    try:
        worktree_id = resolve_worktree_id(manager, worktree_ref)
    except click.ClickException as e:
        raise SystemExit(1) from e

    result = manager.claim(worktree_id, session_id)
    if result:
        click.echo(f"Claimed worktree {worktree_id} for session {session_id}")
    else:
        click.echo(f"Failed to claim worktree {worktree_id}", err=True)


@worktrees.command("release")
@click.argument("worktree_ref")
def release_worktree(worktree_ref: str) -> None:
    """Release a worktree (UUID or prefix)."""
    manager = get_worktree_manager()
    try:
        worktree_id = resolve_worktree_id(manager, worktree_ref)
    except click.ClickException as e:
        raise SystemExit(1) from e

    result = manager.release(worktree_id)
    if result:
        click.echo(f"Released worktree {worktree_id}")
    else:
        click.echo(f"Failed to release worktree {worktree_id}", err=True)


@worktrees.command("sync")
@click.argument("worktree_ref")
@click.option(
    "--source", "-s", "source_branch", help="Source branch to sync from (default: base branch)"
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def sync_worktree(worktree_ref: str, source_branch: str | None, json_format: bool) -> None:
    """Sync worktree with its base branch (UUID or prefix)."""
    manager = get_worktree_manager()
    try:
        worktree_id = resolve_worktree_id(manager, worktree_ref)
    except click.ClickException as e:
        click.echo(str(e), err=True)
        return

    daemon_url = get_daemon_url()

    arguments = {"worktree_id": worktree_id}
    if source_branch:
        arguments["source_branch"] = source_branch

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-worktrees/tools/sync_worktree",
            json=arguments,
            timeout=60.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        return
    except httpx.HTTPStatusError as e:
        click.echo(f"HTTP Error {e.response.status_code}: {e.response.text}", err=True)
        return
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return

    if json_format:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    if result.get("success"):
        click.echo(f"Synced worktree {worktree_id}")
        if result.get("commits_behind"):
            click.echo(f"  Commits merged: {result['commits_behind']}")
    else:
        click.echo(f"Failed to sync worktree: {result.get('error')}", err=True)


# ... stale/cleanup/stats commands ...


def resolve_worktree_id(manager: LocalWorktreeManager, worktree_ref: str) -> str:
    """Resolve worktree reference (UUID or prefix) to full ID."""
    # Optimization: check 36 chars?
    if len(worktree_ref) == 36 and manager.get(worktree_ref):
        return worktree_ref

    # Use list listing since local manager doesn't expose prefix search easily
    all_worktrees = manager.list_worktrees()
    matches = [w for w in all_worktrees if w.id.startswith(worktree_ref)]

    if not matches:
        raise click.ClickException(f"Worktree not found: {worktree_ref}")

    if len(matches) > 1:
        click.echo(f"Ambiguous worktree reference '{worktree_ref}' matches:", err=True)
        for w in matches:
            click.echo(f"  {w.id[:8]} ({w.branch_name})", err=True)
        raise click.ClickException(f"Ambiguous worktree reference: {worktree_ref}")

    return matches[0].id


@worktrees.command("stale")
@click.option("--days", "-d", default=7, help="Days of inactivity to consider stale")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def detect_stale(days: int, json_format: bool) -> None:
    """Detect stale worktrees."""
    daemon_url = get_daemon_url()
    # Convert days to hours for MCP tool
    hours = days * 24

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-worktrees/tools/detect_stale_worktrees",
            json={"hours": hours},
            timeout=30.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        return
    except httpx.HTTPStatusError as e:
        click.echo(f"HTTP Error {e.response.status_code}: {e.response.text}", err=True)
        return
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return

    if json_format:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    stale = result.get("stale_worktrees", [])
    if not stale:
        click.echo(f"No stale worktrees found (inactive > {days} days)")
        return

    click.echo(f"Found {len(stale)} stale worktree(s) (inactive > {days} days):\n")
    for wt in stale:
        click.echo(
            f"  {wt['id']}: {wt['branch_name']} (last updated: {wt.get('updated_at', 'unknown')})"
        )


@worktrees.command("cleanup")
@click.option("--days", "-d", default=7, help="Days of inactivity to consider stale")
@click.option("--dry-run", is_flag=True, help="Show what would be cleaned up")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def cleanup_worktrees(days: int, dry_run: bool, yes: bool) -> None:
    """Clean up stale worktrees."""
    daemon_url = get_daemon_url()
    # Convert days to hours for MCP tool
    hours = days * 24

    if dry_run:
        # Just detect stale - no confirmation needed
        try:
            response = httpx.post(
                f"{daemon_url}/mcp/gobby-worktrees/tools/detect_stale_worktrees",
                json={"hours": hours},
                timeout=30.0,
            )
            response.raise_for_status()
            result = response.json()
            stale = result.get("stale_worktrees", [])
            click.echo(f"Would cleanup {len(stale)} stale worktree(s)")
            for wt in stale:
                click.echo(f"  {wt['id']}: {wt['branch_name']}")
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
        return

    # Confirm before actual cleanup unless --yes is provided
    if not yes:
        click.confirm("Are you sure you want to cleanup stale worktrees?", abort=True)

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-worktrees/tools/cleanup_stale_worktrees",
            json={"hours": hours, "dry_run": False},
            timeout=120.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        return
    except httpx.HTTPStatusError as e:
        click.echo(f"HTTP Error {e.response.status_code}: {e.response.text}", err=True)
        return
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return

    if result.get("success"):
        cleaned = result.get("count", 0)
        click.echo(f"Cleaned up {cleaned} stale worktree(s)")
    else:
        click.echo(f"Failed to cleanup worktrees: {result.get('error')}", err=True)


@worktrees.command("stats")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def worktree_stats(json_format: bool) -> None:
    """Show worktree statistics."""
    import os

    daemon_url = get_daemon_url()

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-worktrees/tools/get_worktree_stats",
            json={"project_path": os.getcwd()},
            timeout=10.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        return
    except httpx.HTTPStatusError as e:
        click.echo(f"HTTP Error {e.response.status_code}: {e.response.text}", err=True)
        return
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return

    if json_format:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    counts = result.get("counts", {})
    total = result.get("total", 0)
    click.echo("Worktree Statistics:")
    click.echo(f"  Total: {total}")
    click.echo(f"  Active: {counts.get('active', 0)}")
    click.echo(f"  Stale: {counts.get('stale', 0)}")
    click.echo(f"  Merged: {counts.get('merged', 0)}")
    click.echo(f"  Abandoned: {counts.get('abandoned', 0)}")
    click.echo(f"  With Sessions: {counts.get('with_sessions', 0)}")
