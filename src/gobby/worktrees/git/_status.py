"""Worktree introspection: status, listing, pruning."""

from __future__ import annotations

import logging
from pathlib import Path

from gobby.worktrees.git._models import GitOperationResult, WorktreeInfo, WorktreeStatus
from gobby.worktrees.git._runner import GitRunner

logger = logging.getLogger(__name__)


def get_worktree_status(
    runner: GitRunner,
    worktree_path: str | Path,
) -> WorktreeStatus | None:
    """
    Get status of a worktree.

    Args:
        worktree_path: Path to the worktree

    Returns:
        WorktreeStatus or None if path is not valid
    """
    worktree_path = Path(worktree_path)

    if not worktree_path.exists():
        return None

    try:
        # Get current branch
        branch_result = runner._run_git(
            ["branch", "--show-current"],
            cwd=worktree_path,
            timeout=5,
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None

        # Get current commit
        commit_result = runner._run_git(
            ["rev-parse", "--short", "HEAD"],
            cwd=worktree_path,
            timeout=5,
        )
        commit = commit_result.stdout.strip() if commit_result.returncode == 0 else None

        # Get status (porcelain for parsing)
        status_result = runner._run_git(
            ["status", "--porcelain"],
            cwd=worktree_path,
            timeout=10,
        )

        has_staged = False
        has_uncommitted = False
        has_untracked = False

        if status_result.returncode == 0:
            for line in status_result.stdout.split("\n"):
                if not line:
                    continue
                index_status = line[0] if len(line) > 0 else " "
                worktree_status = line[1] if len(line) > 1 else " "

                if index_status != " " and index_status != "?":
                    has_staged = True
                if worktree_status != " " and worktree_status != "?":
                    has_uncommitted = True
                if index_status == "?" or worktree_status == "?":
                    has_untracked = True

        # Get ahead/behind count
        ahead = 0
        behind = 0

        if branch:
            # Try to get upstream info
            upstream_result = runner._run_git(
                ["rev-list", "--count", "--left-right", f"origin/{branch}...HEAD"],
                cwd=worktree_path,
                timeout=10,
            )
            if upstream_result.returncode == 0:
                parts = upstream_result.stdout.strip().split("\t")
                if len(parts) == 2:
                    behind = int(parts[0])
                    ahead = int(parts[1])

        return WorktreeStatus(
            has_uncommitted_changes=has_uncommitted,
            has_staged_changes=has_staged,
            has_untracked_files=has_untracked,
            ahead=ahead,
            behind=behind,
            branch=branch,
            commit=commit,
        )

    except Exception as e:
        logger.error(f"Error getting worktree status: {e}")
        return None


def list_worktrees(runner: GitRunner) -> list[WorktreeInfo]:
    """
    List all worktrees for this repository.

    Returns:
        List of WorktreeInfo objects
    """
    try:
        result = runner._run_git(
            ["worktree", "list", "--porcelain"],
            timeout=10,
        )

        if result.returncode != 0:
            logger.error(f"Failed to list worktrees: {result.stderr}")
            return []

        worktrees = []
        current: dict[str, str | bool] = {}

        for line in result.stdout.split("\n"):
            if not line:
                if current:
                    branch_val = current.get("branch")
                    worktrees.append(
                        WorktreeInfo(
                            path=str(current.get("worktree", "")),
                            branch=branch_val if isinstance(branch_val, str) else None,
                            commit=str(current.get("HEAD", "")),
                            is_bare=bool(current.get("bare")),
                            is_detached=bool(current.get("detached")),
                            locked=bool(current.get("locked")),
                            prunable=bool(current.get("prunable")),
                        )
                    )
                    current = {}
                continue

            if line.startswith("worktree "):
                current["worktree"] = line[9:]
            elif line.startswith("HEAD "):
                current["HEAD"] = line[5:]
            elif line.startswith("branch "):
                # refs/heads/branch-name -> branch-name
                branch_ref = line[7:]
                if branch_ref.startswith("refs/heads/"):
                    current["branch"] = branch_ref[11:]
                else:
                    current["branch"] = branch_ref
            elif line == "bare":
                current["bare"] = True
            elif line == "detached":
                current["detached"] = True
            elif line.startswith("locked"):
                current["locked"] = True
            elif line.startswith("prunable"):
                current["prunable"] = True

        # Handle last entry
        if current:
            branch_val = current.get("branch")
            worktrees.append(
                WorktreeInfo(
                    path=str(current.get("worktree", "")),
                    branch=branch_val if isinstance(branch_val, str) else None,
                    commit=str(current.get("HEAD", "")),
                    is_bare=bool(current.get("bare")),
                    is_detached=bool(current.get("detached")),
                    locked=bool(current.get("locked")),
                    prunable=bool(current.get("prunable")),
                )
            )

        return worktrees

    except Exception as e:
        logger.error(f"Error listing worktrees: {e}")
        return []


def prune_worktrees(runner: GitRunner) -> GitOperationResult:
    """
    Prune stale worktree entries.

    Returns:
        GitOperationResult with success status
    """
    try:
        result = runner._run_git(["worktree", "prune"], timeout=30)

        if result.returncode == 0:
            return GitOperationResult(
                success=True,
                message="Pruned stale worktree entries",
                output=result.stdout,
            )
        else:
            return GitOperationResult(
                success=False,
                message=f"Failed to prune: {result.stderr}",
                error=result.stderr,
            )

    except Exception as e:
        return GitOperationResult(
            success=False,
            message=f"Error pruning worktrees: {e}",
            error=str(e),
        )
