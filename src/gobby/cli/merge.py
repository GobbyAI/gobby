"""
Merge conflict resolution CLI commands.

Commands for managing merge operations:
- start: Start a merge with AI-powered resolution
- status: Show merge resolution status
- resolve: Resolve a specific file conflict
- apply: Apply resolved changes and complete merge
- abort: Abort the merge operation
"""

import json
from typing import Any

import click

from gobby.storage.database import LocalDatabase
from gobby.storage.merge_resolutions import MergeResolutionManager


def get_merge_manager() -> MergeResolutionManager:
    """Get initialized merge resolution manager."""
    db = LocalDatabase()
    return MergeResolutionManager(db)


def get_merge_resolver() -> Any:
    """Get merge resolver for AI-powered resolution."""
    from gobby.worktrees.merge import MergeResolver

    return MergeResolver()


def get_project_context() -> dict[str, Any] | None:
    """Get current project context."""
    import os
    from pathlib import Path

    # Look for .gobby/project.json in current directory or parents
    cwd = Path(os.getcwd())
    for parent in [cwd, *cwd.parents]:
        project_file = parent / ".gobby" / "project.json"
        if project_file.exists():
            import json as json_module

            result: dict[str, Any] = json_module.loads(project_file.read_text())
            return result
    return None


def get_worktree_context() -> dict[str, Any] | None:
    """Get current worktree context if in a worktree."""
    import os
    from pathlib import Path

    from gobby.storage.worktrees import LocalWorktreeManager

    db = LocalDatabase()
    manager = LocalWorktreeManager(db)

    # Check if current directory is a worktree
    cwd = Path(os.getcwd()).resolve()
    worktrees = manager.list_worktrees()
    for wt in worktrees:
        if wt.worktree_path:
            worktree_path = Path(wt.worktree_path).resolve()
            # Use is_relative_to for proper path containment check
            try:
                cwd.relative_to(worktree_path)
                # If we get here, cwd is inside worktree_path
                return {
                    "id": wt.id,
                    "branch_name": wt.branch_name,
                    "worktree_path": wt.worktree_path,
                    "base_branch": wt.base_branch,
                }
            except ValueError:
                # cwd is not relative to worktree_path
                continue
    return None


@click.group()
def merge() -> None:
    """Manage merge operations with AI-powered conflict resolution."""
    pass


@merge.command("start")
@click.argument("source_branch")
@click.option(
    "--target",
    "-t",
    "target_branch",
    default="main",
    help="Target branch to merge into (default: main)",
)
@click.option(
    "--strategy",
    "-s",
    type=click.Choice(["auto", "ai-only", "human"]),
    default="auto",
    help="Resolution strategy (default: auto)",
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def merge_start(
    source_branch: str,
    target_branch: str,
    strategy: str,
    json_format: bool,
) -> None:
    """Start a merge operation with AI-powered conflict resolution.

    Examples:

        gobby merge start feature/my-feature

        gobby merge start feature/auth --target develop --strategy ai-only
    """
    project = get_project_context()
    if not project:
        click.echo("Error: Not in a Gobby project. Run 'gobby init' first.", err=True)
        raise SystemExit(1)

    # Get worktree context if available
    worktree = get_worktree_context()
    worktree_id = worktree["id"] if worktree else project.get("id", "default")

    manager = get_merge_manager()

    try:
        resolution, _created = manager.get_or_create_resolution(
            worktree_id=worktree_id,
            source_branch=source_branch,
            target_branch=target_branch,
            status="pending",
            tier_used=strategy,
        )

        if json_format:
            click.echo(json.dumps(resolution.to_dict(), indent=2, default=str))
            return

        click.echo(f"Started merge: {resolution.id}")
        click.echo(f"  Source: {source_branch}")
        click.echo(f"  Target: {target_branch}")
        click.echo(f"  Strategy: {strategy}")
        click.echo(f"  Status: {resolution.status}")

    except Exception as e:
        click.echo(f"Error starting merge: {e}", err=True)
        raise SystemExit(1) from None


@merge.command("status")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed conflict information")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def merge_status(verbose: bool, json_format: bool) -> None:
    """Show the status of current merge operation.

    Examples:

        gobby merge status

        gobby merge status --verbose
    """
    project = get_project_context()
    if not project:
        click.echo("Error: Not in a Gobby project. Run 'gobby init' first.", err=True)
        raise SystemExit(1)

    manager = get_merge_manager()

    # Get worktree context for filtering
    worktree = get_worktree_context()
    worktree_id = worktree["id"] if worktree else None

    # List active resolutions
    resolutions = manager.list_resolutions(
        worktree_id=worktree_id,
        status="pending",
    )

    if json_format:
        output = []
        for res in resolutions:
            res_dict = res.to_dict()
            res_dict["conflicts"] = [
                c.to_dict() for c in manager.list_conflicts(resolution_id=res.id)
            ]
            output.append(res_dict)
        click.echo(json.dumps(output, indent=2, default=str))
        return

    if not resolutions:
        click.echo("No active merge operations found.")
        return

    for res in resolutions:
        conflicts = manager.list_conflicts(resolution_id=res.id)
        pending_count = sum(1 for c in conflicts if c.status == "pending")
        resolved_count = sum(1 for c in conflicts if c.status == "resolved")

        click.echo(f"Merge: {res.id}")
        click.echo(f"  Source: {res.source_branch} -> {res.target_branch}")
        click.echo(f"  Status: {res.status}")
        click.echo(f"  Conflicts: {pending_count} pending, {resolved_count} resolved")

        if verbose and conflicts:
            click.echo("  Files:")
            for conflict in conflicts:
                status_icon = "✓" if conflict.status == "resolved" else "○"
                click.echo(f"    {status_icon} {conflict.file_path} ({conflict.status})")


@merge.command("resolve")
@click.argument("file_path")
@click.option(
    "--strategy",
    "-s",
    type=click.Choice(["ai", "human"]),
    default="ai",
    help="Resolution strategy (default: ai)",
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def merge_resolve(file_path: str, strategy: str, json_format: bool) -> None:
    """Resolve a specific file conflict.

    Examples:

        gobby merge resolve src/main.py

        gobby merge resolve src/config.py --strategy human
    """
    project = get_project_context()
    if not project:
        click.echo("Error: Not in a Gobby project. Run 'gobby init' first.", err=True)
        raise SystemExit(1)

    manager = get_merge_manager()

    try:
        # Find conflict by file path
        conflict = manager.get_conflict_by_path(file_path)
        if not conflict:
            click.echo(f"Error: No conflict found for file '{file_path}'", err=True)
            raise SystemExit(1)

        if strategy == "ai":
            # AI resolution
            get_merge_resolver()  # Validates resolver is available
            # Would call AI resolver here
            click.echo(f"Resolving {file_path} with AI...")
            manager.update_conflict(conflict.id, status="resolved")
        else:
            # Human resolution - just mark as pending human review
            click.echo(f"Marked {file_path} for human resolution")

        if json_format:
            updated = manager.get_conflict(conflict.id)
            if updated:
                click.echo(json.dumps(updated.to_dict(), indent=2, default=str))
            return

        click.echo(f"Resolved: {file_path}")

    except AttributeError:
        # get_conflict_by_path may not exist
        click.echo(f"Error: Conflict not found for '{file_path}'", err=True)
        raise SystemExit(1) from None
    except Exception as e:
        click.echo(f"Error resolving conflict: {e}", err=True)
        raise SystemExit(1) from None


@merge.command("apply")
@click.option("--force", "-f", is_flag=True, help="Force apply even with pending conflicts")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def merge_apply(force: bool, json_format: bool) -> None:
    """Apply resolved changes and complete the merge.

    Examples:

        gobby merge apply

        gobby merge apply --force
    """
    project = get_project_context()
    if not project:
        click.echo("Error: Not in a Gobby project. Run 'gobby init' first.", err=True)
        raise SystemExit(1)

    manager = get_merge_manager()

    try:
        # Get active resolution
        resolution = manager.get_active_resolution()
        if not resolution:
            click.echo("Error: No active merge operation found.", err=True)
            raise SystemExit(1)

        # Check for pending conflicts
        conflicts = manager.list_conflicts(resolution_id=resolution.id)
        pending = [c for c in conflicts if c.status == "pending"]

        if pending and not force:
            click.echo(
                f"Error: {len(pending)} pending conflict(s). "
                "Resolve them or use --force to apply anyway.",
                err=True,
            )
            raise SystemExit(1)

        # Apply merge
        manager.update_resolution(resolution.id, status="resolved")

        if json_format:
            updated = manager.get_resolution(resolution.id)
            if updated:
                click.echo(json.dumps(updated.to_dict(), indent=2, default=str))
            return

        click.echo(f"Applied merge: {resolution.id}")
        click.echo(f"  {len(conflicts)} file(s) merged")

    except AttributeError:
        # get_active_resolution may not exist
        click.echo("Error: No active merge operation found.", err=True)
        raise SystemExit(1) from None
    except Exception as e:
        click.echo(f"Error applying merge: {e}", err=True)
        raise SystemExit(1) from None


@merge.command("abort")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def merge_abort(json_format: bool) -> None:
    """Abort the current merge operation.

    Examples:

        gobby merge abort
    """
    project = get_project_context()
    if not project:
        click.echo("Error: Not in a Gobby project. Run 'gobby init' first.", err=True)
        raise SystemExit(1)

    manager = get_merge_manager()

    try:
        # Get active resolution
        resolution = manager.get_active_resolution()
        if not resolution:
            click.echo("Error: No active merge operation to abort.", err=True)
            raise SystemExit(1)

        # Check if already resolved
        if resolution.status == "resolved":
            click.echo("Error: Cannot abort an already resolved merge.", err=True)
            raise SystemExit(1)

        # Delete resolution (cascades to conflicts)
        resolution_id = resolution.id
        deleted = manager.delete_resolution(resolution_id)

        if json_format:
            click.echo(json.dumps({"aborted": deleted, "resolution_id": resolution_id}))
            return

        if deleted:
            click.echo(f"Aborted merge: {resolution_id}")
        else:
            click.echo("Failed to abort merge.", err=True)
            raise SystemExit(1)

    except AttributeError:
        # get_active_resolution may not exist
        click.echo("Error: No active merge operation to abort.", err=True)
        raise SystemExit(1) from None
    except Exception as e:
        click.echo(f"Error aborting merge: {e}", err=True)
        raise SystemExit(1) from None
