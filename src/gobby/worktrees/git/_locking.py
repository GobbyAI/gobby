"""Worktree lock/unlock operations."""

from __future__ import annotations

from pathlib import Path

from gobby.worktrees.git._models import GitOperationResult
from gobby.worktrees.git._runner import GitRunner


def lock_worktree(
    runner: GitRunner,
    worktree_path: str | Path,
    reason: str | None = None,
) -> GitOperationResult:
    """
    Lock a worktree to prevent accidental pruning.

    Args:
        worktree_path: Path to the worktree
        reason: Optional reason for locking

    Returns:
        GitOperationResult with success status
    """
    args = ["worktree", "lock", str(worktree_path)]
    if reason:
        args.extend(["--reason", reason])

    try:
        result = runner._run_git(args, timeout=10)

        if result.returncode == 0:
            return GitOperationResult(
                success=True,
                message=f"Locked worktree at {worktree_path}",
            )
        else:
            return GitOperationResult(
                success=False,
                message=f"Failed to lock: {result.stderr}",
                error=result.stderr,
            )

    except Exception as e:
        return GitOperationResult(
            success=False,
            message=f"Error locking worktree: {e}",
            error=str(e),
        )


def unlock_worktree(runner: GitRunner, worktree_path: str | Path) -> GitOperationResult:
    """
    Unlock a worktree.

    Args:
        worktree_path: Path to the worktree

    Returns:
        GitOperationResult with success status
    """
    try:
        result = runner._run_git(
            ["worktree", "unlock", str(worktree_path)],
            timeout=10,
        )

        if result.returncode == 0:
            return GitOperationResult(
                success=True,
                message=f"Unlocked worktree at {worktree_path}",
            )
        else:
            return GitOperationResult(
                success=False,
                message=f"Failed to unlock: {result.stderr}",
                error=result.stderr,
            )

    except Exception as e:
        return GitOperationResult(
            success=False,
            message=f"Error unlocking worktree: {e}",
            error=str(e),
        )
