"""Worktree CRUD: create, delete, sync."""

from __future__ import annotations

import logging
import shutil
import subprocess  # nosec B404 # subprocess.TimeoutExpired re-raised from runner
from pathlib import Path
from typing import Literal

from gobby.worktrees.git._models import GitOperationResult
from gobby.worktrees.git._runner import GitRunner
from gobby.worktrees.git._status import get_worktree_status

logger = logging.getLogger(__name__)


def create_worktree(
    runner: GitRunner,
    worktree_path: str | Path,
    branch_name: str,
    base_branch: str = "main",
    create_branch: bool = True,
    use_local: bool = False,
) -> GitOperationResult:
    """
    Create a new git worktree.

    Args:
        worktree_path: Path where worktree will be created
        branch_name: Name of the branch for the worktree
        base_branch: Branch to base the new branch on (if create_branch=True)
        create_branch: Whether to create a new branch or use existing
        use_local: If True, create from local branch ref instead of origin/
                   This preserves unpushed commits in the worktree.

    Returns:
        GitOperationResult with success status and message
    """
    worktree_path = Path(worktree_path)

    # Check if path already exists
    if worktree_path.exists():
        return GitOperationResult(
            success=False,
            message=f"Path already exists: {worktree_path}",
        )

    # Ensure parent directory exists
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if create_branch:
            if use_local:
                # Create worktree from local branch (preserves unpushed commits)
                # Verify local branch exists
                verify_result = runner._run_git(["rev-parse", "--verify", base_branch], timeout=5)
                if verify_result.returncode != 0:
                    return GitOperationResult(
                        success=False,
                        message=f"Local branch '{base_branch}' not found",
                        error=verify_result.stderr,
                    )

                # Create worktree with new branch based on local ref
                result = runner._run_git(
                    [
                        "worktree",
                        "add",
                        "-b",
                        branch_name,
                        str(worktree_path),
                        base_branch,  # Local ref, not origin/
                    ],
                    timeout=60,
                )
            else:
                # Create worktree with new branch based on origin (original behavior)
                # First, fetch to ensure we have latest refs
                fetch_result = runner._run_git(["fetch", "origin", base_branch], timeout=60)
                if fetch_result.returncode != 0:
                    return GitOperationResult(
                        success=False,
                        message=f"Failed to fetch origin/{base_branch}: {fetch_result.stderr}",
                        error=fetch_result.stderr,
                    )

                # Create worktree with new branch
                result = runner._run_git(
                    [
                        "worktree",
                        "add",
                        "-b",
                        branch_name,
                        str(worktree_path),
                        f"origin/{base_branch}",
                    ],
                    timeout=60,
                )
        else:
            # Use existing branch
            result = runner._run_git(
                ["worktree", "add", str(worktree_path), branch_name],
                timeout=60,
            )

        if create_branch and result.returncode != 0 and _is_branch_exists_error(result.stderr):
            logger.info(
                "Branch %s already exists while creating worktree; reusing existing branch",
                branch_name,
            )
            result = runner._run_git(
                ["worktree", "add", str(worktree_path), branch_name],
                timeout=60,
            )

        if result.returncode == 0:
            return GitOperationResult(
                success=True,
                message=f"Created worktree at {worktree_path}",
                output=result.stdout,
            )
        else:
            return GitOperationResult(
                success=False,
                message=f"Failed to create worktree: {result.stderr}",
                error=result.stderr,
            )

    except subprocess.TimeoutExpired:
        return GitOperationResult(
            success=False,
            message="Git command timed out",
        )
    except Exception as e:
        return GitOperationResult(
            success=False,
            message=f"Error creating worktree: {e}",
            error=str(e),
        )


def _is_branch_exists_error(stderr: str | None) -> bool:
    if not stderr:
        return False
    normalized = stderr.lower()
    return "branch" in normalized and "already exists" in normalized


def delete_worktree(
    runner: GitRunner,
    worktree_path: str | Path,
    force: bool = False,
    delete_branch: bool = False,
    branch_name: str | None = None,
) -> GitOperationResult:
    """
    Delete a git worktree.

    Args:
        worktree_path: Path to the worktree to delete
        force: Force removal even if dirty
        delete_branch: Also delete the associated branch
        branch_name: Optional explicit branch name (if not provided, attempts to discover)

    Returns:
        GitOperationResult with success status and message
    """
    worktree_path = Path(worktree_path)

    try:
        # Get branch name before removal (for optional branch deletion)
        if delete_branch and not branch_name:
            status = get_worktree_status(runner, worktree_path)
            if status:
                branch_name = status.branch
            if not branch_name:
                logger.warning(
                    f"Branch deletion skipped: branch_name could not be resolved from get_worktree_status for worktree '{worktree_path}'",
                    extra={"worktree_path": str(worktree_path), "delete_branch": True},
                )

        # Remove worktree
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree_path))

        result = runner._run_git(args, timeout=30)

        if result.returncode != 0:
            # git worktree remove --force only handles modified tracked files,
            # not untracked files (.mypy_cache, .ruff_cache, __pycache__, etc).
            # Fall back to manual removal + prune when force is requested.
            if force and worktree_path.exists():
                logger.warning(
                    "git worktree remove failed for %s; falling back to shutil.rmtree + worktree prune. stderr=%s",
                    worktree_path,
                    result.stderr.strip(),
                )
                shutil.rmtree(worktree_path, ignore_errors=True)
                runner._run_git(["worktree", "prune"], timeout=10)
                if not worktree_path.exists():
                    logger.info(
                        f"Removed worktree via fallback (rmtree + prune): {worktree_path}",
                    )
                else:
                    return GitOperationResult(
                        success=False,
                        message=f"Failed to remove worktree even with fallback: {result.stderr}",
                        error=result.stderr,
                    )
            else:
                return GitOperationResult(
                    success=False,
                    message=f"Failed to remove worktree: {result.stderr}",
                    error=result.stderr,
                )

        # Optionally delete the branch
        if delete_branch and branch_name:
            branch_result = runner._run_git(
                ["branch", "-D" if force else "-d", branch_name],
                timeout=10,
            )

            if branch_result.returncode != 0:
                return GitOperationResult(
                    success=True,  # Worktree removed, but branch deletion failed
                    message=f"Worktree removed, but failed to delete branch: {branch_result.stderr}",
                    error=branch_result.stderr,
                )

        return GitOperationResult(
            success=True,
            message=f"Deleted worktree at {worktree_path}"
            + (f" and branch {branch_name}" if delete_branch and branch_name else ""),
            output=result.stdout,
        )

    except subprocess.TimeoutExpired:
        return GitOperationResult(
            success=False,
            message="Git command timed out",
        )
    except Exception as e:
        return GitOperationResult(
            success=False,
            message=f"Error deleting worktree: {e}",
            error=str(e),
        )


def sync_from_main(
    runner: GitRunner,
    worktree_path: str | Path,
    base_branch: str = "main",
    strategy: Literal["rebase", "merge"] = "rebase",
) -> GitOperationResult:
    """
    Sync worktree with base branch.

    Args:
        worktree_path: Path to the worktree
        base_branch: Branch to sync from
        strategy: Sync strategy (rebase or merge)

    Returns:
        GitOperationResult with success status and message
    """
    worktree_path = Path(worktree_path)

    if not worktree_path.exists():
        return GitOperationResult(
            success=False,
            message=f"Worktree path does not exist: {worktree_path}",
        )

    try:
        # Fetch latest from origin
        fetch_result = runner._run_git(
            ["fetch", "origin", base_branch],
            cwd=worktree_path,
            timeout=60,
        )
        if fetch_result.returncode != 0:
            return GitOperationResult(
                success=False,
                message=f"Failed to fetch: {fetch_result.stderr}",
                error=fetch_result.stderr,
            )

        # Perform rebase or merge
        if strategy == "rebase":
            sync_result = runner._run_git(
                ["rebase", f"origin/{base_branch}"],
                cwd=worktree_path,
                timeout=120,
            )
        else:
            sync_result = runner._run_git(
                ["merge", f"origin/{base_branch}", "--no-edit"],
                cwd=worktree_path,
                timeout=120,
            )

        if sync_result.returncode != 0:
            # Check if there are conflicts
            if "CONFLICT" in sync_result.stdout or "CONFLICT" in sync_result.stderr:
                try:
                    abort_result = runner._run_git(
                        [strategy, "--abort"],
                        cwd=worktree_path,
                        timeout=30,
                    )
                    aborted = abort_result.returncode == 0
                    abort_error = abort_result.stderr.strip() or abort_result.stdout.strip()
                except Exception as e:
                    aborted = False
                    abort_error = str(e)
                abort_detail = "; aborted" if aborted else f"; abort failed: {abort_error}"
                return GitOperationResult(
                    success=False,
                    message=f"Sync failed due to conflicts{abort_detail}",
                    error=sync_result.stderr or sync_result.stdout,
                )
            return GitOperationResult(
                success=False,
                message=f"Failed to {strategy}: {sync_result.stderr}",
                error=sync_result.stderr,
            )

        return GitOperationResult(
            success=True,
            message=f"Successfully synced with origin/{base_branch} using {strategy}",
            output=sync_result.stdout,
        )

    except subprocess.TimeoutExpired:
        return GitOperationResult(
            success=False,
            message="Git command timed out",
        )
    except Exception as e:
        return GitOperationResult(
            success=False,
            message=f"Error syncing worktree: {e}",
            error=str(e),
        )
