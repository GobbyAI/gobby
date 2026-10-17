"""Merge a clone's branch into a target branch."""

from __future__ import annotations

import asyncio
import logging
import subprocess  # nosec B404 # subprocess needed for git merge operations
from pathlib import Path

from gobby.clones.git import CloneGitManager, GitOperationResult

logger = logging.getLogger(__name__)


async def merge_clone_branch(
    manager: CloneGitManager,
    source_branch: str,
    target_branch: str = "main",
    working_dir: str | Path | None = None,
    source_is_local: bool = False,
) -> GitOperationResult:
    """
    Merge source branch into target branch.

    Performs:
    1. Fetch latest from remote
    2. Checkout target branch
    3. Attempt merge from source branch

    Args:
        manager: Clone git manager whose repository runs the merge
        source_branch: Branch to merge from
        target_branch: Branch to merge into (default: main)
        working_dir: Working directory (defaults to repo_path)
        source_is_local: Merge the local source branch instead of origin/<source>

    Returns:
        GitOperationResult with success status and conflict info
    """
    cwd = Path(working_dir) if working_dir else manager.repo_path

    if not cwd.exists():
        return GitOperationResult(
            success=False,
            message=f"Working directory does not exist: {cwd}",
            error="directory_not_found",
        )

    # Save current branch so we can restore on failure
    original_branch: str | None = None
    branch_result = await manager._run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd,
        timeout=5,
    )
    if branch_result.returncode == 0:
        original_branch = branch_result.stdout.strip()
        if original_branch == "HEAD":
            commit_result = await manager._run_git(
                ["rev-parse", "HEAD"],
                cwd=cwd,
                timeout=5,
            )
            if commit_result.returncode == 0:
                original_branch = commit_result.stdout.strip()
            else:
                return GitOperationResult(
                    success=False,
                    message=f"Failed to resolve detached HEAD: {commit_result.stderr}",
                    error=commit_result.stderr,
                )

    checked_out_target = False

    async def _best_effort_cleanup(args: list[str], operation: str) -> None:
        try:
            result = await manager._run_git(args, cwd=cwd, timeout=10)
        except (subprocess.SubprocessError, OSError):
            logger.warning("Failed to %s in %s", operation, cwd, exc_info=True)
            return
        if result.returncode != 0:
            logger.warning(
                "Failed to %s in %s: %s",
                operation,
                cwd,
                result.stderr.strip() or result.stdout.strip(),
            )

    try:
        # Fetch latest
        fetch_result = await manager._run_git(
            ["fetch", "origin"],
            cwd=cwd,
            timeout=60,
        )
        if fetch_result.returncode != 0:
            return GitOperationResult(
                success=False,
                message=f"Failed to fetch: {fetch_result.stderr}",
                error=fetch_result.stderr,
            )

        # Checkout target branch
        checkout_result = await manager._run_git(
            ["checkout", target_branch],
            cwd=cwd,
            timeout=30,
        )
        if checkout_result.returncode != 0:
            return GitOperationResult(
                success=False,
                message=f"Failed to checkout {target_branch}: {checkout_result.stderr}",
                error=checkout_result.stderr,
            )
        checked_out_target = True

        # Pull latest on target
        pull_result = await manager._run_git(
            ["pull", "origin", target_branch],
            cwd=cwd,
            timeout=60,
        )
        if pull_result.returncode != 0:
            return GitOperationResult(
                success=False,
                message=f"Failed to pull {target_branch}: {pull_result.stderr}",
                error=pull_result.stderr,
            )

        # Attempt merge (set GOBBY_MERGE=1 so pre-merge-commit hook skips)
        source_ref = source_branch if source_is_local else f"origin/{source_branch}"
        merge_result = await manager._run_git(
            ["merge", source_ref, "--no-edit"],
            cwd=cwd,
            timeout=60,
            env={"GOBBY_MERGE": "1"},
        )

        if merge_result.returncode != 0:
            # Check if it's a conflict
            if "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr:
                # Get list of conflicted files
                status_result = await manager._run_git(
                    ["diff", "--name-only", "--diff-filter=U"],
                    cwd=cwd,
                    timeout=10,
                )
                conflicted_files = [f for f in status_result.stdout.strip().split("\n") if f]

                # Abort the merge to leave repo in clean state
                await _best_effort_cleanup(["merge", "--abort"], "abort merge")

                return GitOperationResult(
                    success=False,
                    message=f"Merge conflict in {len(conflicted_files)} files",
                    error="merge_conflict",
                    output="\n".join(conflicted_files),
                )

            # Non-conflict merge failure — abort to clean up
            await _best_effort_cleanup(["merge", "--abort"], "abort merge")

            return GitOperationResult(
                success=False,
                message=f"Merge failed: {merge_result.stderr}",
                error=merge_result.stderr,
            )

        return GitOperationResult(
            success=True,
            message=f"Successfully merged {source_branch} into {target_branch}",
            output=merge_result.stdout,
        )

    except asyncio.CancelledError:
        await _best_effort_cleanup(["merge", "--abort"], "abort cancelled merge")
        raise
    except subprocess.TimeoutExpired:
        # Attempt to abort any in-progress merge
        await _best_effort_cleanup(["merge", "--abort"], "abort timed-out merge")
        return GitOperationResult(
            success=False,
            message="Merge operation timed out",
            error="timeout",
        )
    except Exception as e:
        # Attempt to abort any in-progress merge
        await _best_effort_cleanup(["merge", "--abort"], "abort failed merge")
        return GitOperationResult(
            success=False,
            message=f"Merge error: {e}",
            error=str(e),
        )
    finally:
        # Restore original branch if we checked out the target
        if checked_out_target and original_branch and original_branch != target_branch:
            await _best_effort_cleanup(
                ["checkout", original_branch],
                f"restore original branch {original_branch}",
            )
