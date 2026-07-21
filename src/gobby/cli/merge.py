"""
Merge conflict resolution CLI commands.

Commands for managing merge operations:
- start: Start a merge with AI-powered resolution
- status: Show merge resolution status
- resolve: Resolve a specific file conflict
- apply: Apply resolved changes and complete merge
- abort: Abort the merge operation
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click

from gobby.cli.runtime import require_cli_database
from gobby.mcp_proxy.tools.merge_conflict_hydration import conflict_hunks_for_ai
from gobby.storage.merge_resolutions import ConflictStatus, MergeResolutionManager
from gobby.utils.json_helpers import json_dumps


def get_merge_manager() -> MergeResolutionManager:
    """Get initialized merge resolution manager."""
    return MergeResolutionManager(require_cli_database())


def get_merge_resolver() -> Any:
    """Get merge resolver for AI-powered resolution."""
    from gobby.worktrees.merge import MergeResolver

    return MergeResolver()


def get_worktree_manager() -> Any:
    """Get initialized worktree storage manager."""
    from gobby.storage.worktrees import LocalWorktreeManager

    return LocalWorktreeManager(require_cli_database())


@contextmanager
def worktree_manager_context() -> Iterator[Any]:
    """Yield a worktree manager borrowing the CLI database."""
    yield get_worktree_manager()


def get_git_manager(worktree_path: str) -> Any:
    """Get git manager rooted at the worktree path being merged."""
    from gobby.worktrees.git import WorktreeGitManager

    return WorktreeGitManager(worktree_path)


def _get_resolution_worktree_path(
    manager: MergeResolutionManager,
    resolution_id: str,
) -> str:
    resolution = manager.get_resolution(resolution_id)
    if not resolution:
        raise RuntimeError(f"Resolution '{resolution_id}' not found")

    with worktree_manager_context() as worktree_manager:
        worktree = worktree_manager.get(resolution.worktree_id)
    if not worktree or not worktree.worktree_path:
        raise RuntimeError(f"Worktree '{resolution.worktree_id}' not found or has no path")
    return str(worktree.worktree_path)


async def _resolve_conflict_with_ai(
    manager: MergeResolutionManager,
    conflict: Any,
) -> dict[str, Any]:
    worktree_path = _get_resolution_worktree_path(manager, conflict.resolution_id)
    resolver = get_merge_resolver()
    result = await resolver.resolve_file(
        path=conflict.file_path,
        conflict_hunks=await conflict_hunks_for_ai(conflict, worktree_path),
        worktree_path=worktree_path,
    )

    if not result.success:
        return {
            "success": False,
            "error": "AI resolution failed",
            "needs_human_review": result.needs_human_review,
            "failure_reason": result.failure_reason,
        }

    resolved = result.resolved_content_by_file.get(conflict.file_path)
    if not resolved:
        return {
            "success": False,
            "error": (
                f"AI resolver returned success but produced no content for {conflict.file_path}"
            ),
            "needs_human_review": True,
        }

    target = Path(worktree_path) / conflict.file_path
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_text, resolved, encoding="utf-8")
    updated = manager.update_conflict(
        conflict_id=conflict.id,
        status=ConflictStatus.RESOLVED.value,
        resolved_content=resolved,
    )

    return {
        "success": True,
        "conflict": updated.to_dict() if updated else None,
        "resolution_method": "ai",
        "tier": result.tier.value,
    }


async def _apply_active_resolution(
    manager: MergeResolutionManager,
    resolution_id: str,
) -> dict[str, Any]:
    worktree_path = _get_resolution_worktree_path(manager, resolution_id)

    from gobby.mcp_proxy.tools.merge import create_merge_registry

    with worktree_manager_context() as worktree_manager:
        registry = create_merge_registry(
            merge_storage=manager,
            merge_resolver=get_merge_resolver(),
            git_manager=get_git_manager(worktree_path),
            worktree_manager=worktree_manager,
        )
        result = await registry.call("merge_apply", {"resolution_id": resolution_id})
    return result if isinstance(result, dict) else {"success": False, "error": str(result)}


def _echo_tool_error(prefix: str, result: dict[str, Any]) -> None:
    message = result.get("error") or result.get("failure_reason") or "unknown error"
    click.echo(f"{prefix}: {message}", err=True)


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

    db = require_cli_database()
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
            click.echo(json_dumps(resolution.to_dict(), indent=2, default=str))
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
        click.echo(json_dumps(output, indent=2, default=str))
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
            click.echo(f"Resolving {file_path} with AI...")
            result = asyncio.run(_resolve_conflict_with_ai(manager, conflict))
            if not result.get("success"):
                _echo_tool_error("Error resolving conflict", result)
                raise SystemExit(1)
        else:
            # Human resolution - just mark as pending human review
            click.echo(f"Marked {file_path} for human resolution")

        if json_format:
            updated = manager.get_conflict(conflict.id)
            if updated:
                click.echo(json_dumps(updated.to_dict(), indent=2, default=str))
            return

        click.echo(f"Resolved: {file_path}")

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
    worktree = get_worktree_context()
    worktree_id = worktree["id"] if worktree else None

    try:
        # Get active resolution
        resolution = manager.get_active_resolution(worktree_id=worktree_id)
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

        result = asyncio.run(_apply_active_resolution(manager, resolution.id))
        if not result.get("success"):
            if json_format:
                click.echo(json_dumps(result, indent=2, default=str))
            else:
                _echo_tool_error("Error applying merge", result)
            raise SystemExit(1)

        if json_format:
            click.echo(json_dumps(result, indent=2, default=str))
            return

        files_merged = result.get("files_merged", [])
        click.echo(f"Applied merge: {resolution.id}")
        click.echo(f"  {len(files_merged)} file(s) merged")
        if result.get("commit_sha"):
            click.echo(f"  commit: {result['commit_sha']}")

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
    worktree = get_worktree_context()
    worktree_id = worktree["id"] if worktree else None

    try:
        # Get active resolution
        resolution = manager.get_active_resolution(worktree_id=worktree_id)
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
            click.echo(json_dumps({"aborted": deleted, "resolution_id": resolution_id}))
            return

        if deleted:
            click.echo(f"Aborted merge: {resolution_id}")
        else:
            click.echo("Failed to abort merge.", err=True)
            raise SystemExit(1)

    except Exception as e:
        click.echo(f"Error aborting merge: {e}", err=True)
        raise SystemExit(1) from None
