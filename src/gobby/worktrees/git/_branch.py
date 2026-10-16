"""Branch and commit query operations."""

from __future__ import annotations

import logging
import subprocess  # nosec B404 # typing only; execution stays in the runner
from typing import Protocol

from gobby.worktrees.git._runner import GitRunner

logger = logging.getLogger(__name__)


class SupportsRunGit(Protocol):
    """Structural runner contract for read-only default-branch detection."""

    async def _run_git(
        self,
        args: list[str],
        *,
        timeout: int = ...,
    ) -> subprocess.CompletedProcess[str]: ...


async def get_default_branch(runner: SupportsRunGit) -> str:
    """
    Get the default branch for the repository.

    Tries multiple methods to detect the default branch:
    1. Check origin/HEAD symbolic ref (most reliable for cloned repos)
    2. Check for common default branch names (main, master, develop)
    3. Fall back to "main" if detection fails

    Returns:
        Default branch name (e.g., "main", "master", "develop")
    """
    # Method 1: Try to get the default branch from origin/HEAD
    try:
        result = await runner._run_git(
            ["symbolic-ref", "refs/remotes/origin/HEAD"],
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Output is like "refs/remotes/origin/main"
            ref = result.stdout.strip()
            if ref.startswith("refs/remotes/origin/"):
                branch = ref[len("refs/remotes/origin/") :]
                logger.debug("Detected default branch from origin/HEAD: %s", branch)
                return branch
    except Exception as e:
        logger.debug("Method 1 (origin/HEAD) for default branch failed: %s", e)

    # Method 2: Check which common default branches exist
    for branch in ["main", "master", "develop"]:
        try:
            # Check if the branch exists locally or remotely
            result = await runner._run_git(
                ["rev-parse", "--verify", f"refs/heads/{branch}"],
                timeout=5,
            )
            if result.returncode == 0:
                logger.debug("Detected default branch from local ref: %s", branch)
                return branch

            # Check remote
            result = await runner._run_git(
                ["rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
                timeout=5,
            )
            if result.returncode == 0:
                logger.debug("Detected default branch from remote ref: %s", branch)
                return branch
        except Exception as e:
            logger.debug("Method 2 branch check failed for %s: %s", branch, e)
            continue

    # Method 3: Fall back to "main"
    logger.debug("Could not detect default branch, falling back to 'main'")
    return "main"


async def get_current_branch(runner: GitRunner) -> str | None:
    """
    Get the current branch of the repository.

    Returns:
        Branch name, or None if in detached HEAD state

    Raises:
        subprocess.TimeoutExpired: Git could not inspect the branch in time.
    """
    try:
        result = await runner._run_git(
            ["branch", "--show-current"],
            timeout=5,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return branch if branch else None
        return None
    except subprocess.TimeoutExpired:
        raise
    except Exception:
        return None


async def get_local_commit(runner: GitRunner, branch: str) -> str | None:
    """
    Get the commit SHA of a local branch.

    Args:
        branch: Branch name

    Returns:
        Commit SHA, or None if branch doesn't exist
    """
    try:
        result = await runner._run_git(
            ["rev-parse", branch],
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None
