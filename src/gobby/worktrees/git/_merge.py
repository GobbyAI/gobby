"""Branch-level merge orchestration in the main repository."""

from __future__ import annotations

import logging
import subprocess  # nosec B404 # subprocess.TimeoutExpired re-raised from runner

from gobby.worktrees.git._models import GitOperationResult
from gobby.worktrees.git._runner import GitRunner

logger = logging.getLogger(__name__)


def merge_branch(
    runner: GitRunner,
    source_branch: str,
    target_branch: str = "main",
    push: bool = False,
) -> GitOperationResult:
    """
    Merge source branch into target branch in the main repository.

    Performs:
    1. Fetch latest from remote
    2. Checkout target branch
    3. Pull latest on target
    4. Attempt merge from source branch (--no-ff)

    On conflict: aborts merge, restores original branch.

    Args:
        source_branch: Branch to merge from
        target_branch: Branch to merge into (default: main)
        push: Unsupported. Merge automation never pushes to remote.

    Returns:
        GitOperationResult with success status and conflict info
    """
    if push:
        return GitOperationResult(
            success=False,
            message=(
                "merge_branch is local-only and never pushes to remote. "
                "Use the explicit PR delivery flow for remote publication."
            ),
            error="push_not_supported",
        )

    # Save current branch for restoration
    original_branch: str | None = None
    branch_result = runner._run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        timeout=5,
    )
    if branch_result.returncode == 0:
        original_branch = branch_result.stdout.strip()

    checked_out_target = False

    try:
        # Fetch latest
        fetch_result = runner._run_git(
            ["fetch", "origin"],
            timeout=60,
        )
        if fetch_result.returncode != 0:
            return GitOperationResult(
                success=False,
                message=f"Failed to fetch: {fetch_result.stderr}",
                error=fetch_result.stderr,
            )

        # Checkout target branch
        checkout_result = runner._run_git(
            ["checkout", target_branch],
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
        pull_result = runner._run_git(
            ["pull", "origin", target_branch],
            timeout=60,
        )
        if pull_result.returncode != 0:
            return GitOperationResult(
                success=False,
                message=f"Failed to pull {target_branch}: {pull_result.stderr}",
                error=pull_result.stderr,
            )

        # Attempt merge with --no-ff
        merge_result = runner._run_git(
            ["merge", source_branch, "--no-ff", "--no-edit"],
            timeout=60,
        )

        if merge_result.returncode != 0:
            # Check for conflicts
            if "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr:
                # Get conflicted files
                status_result = runner._run_git(
                    ["diff", "--name-only", "--diff-filter=U"],
                    timeout=10,
                )
                conflicted_files = [f for f in status_result.stdout.strip().split("\n") if f]

                # Abort merge
                runner._run_git(["merge", "--abort"], timeout=10)

                return GitOperationResult(
                    success=False,
                    message=f"Merge conflict in {len(conflicted_files)} files",
                    error="merge_conflict",
                    output="\n".join(conflicted_files),
                )

            # Non-conflict failure — abort
            runner._run_git(["merge", "--abort"], timeout=10)

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

    except subprocess.TimeoutExpired:
        runner._run_git(["merge", "--abort"], timeout=10)
        return GitOperationResult(
            success=False,
            message="Merge operation timed out",
            error="timeout",
        )
    except Exception as e:
        runner._run_git(["merge", "--abort"], timeout=10)
        return GitOperationResult(
            success=False,
            message=f"Merge error: {e}",
            error=str(e),
        )
    finally:
        # Restore original branch if we checked out target
        if checked_out_target and original_branch and original_branch != target_branch:
            try:
                runner._run_git(
                    ["checkout", original_branch],
                    timeout=30,
                )
            except Exception:
                logger.error(
                    "Failed to restore original branch %s after merge attempt",
                    original_branch,
                    exc_info=True,
                )
