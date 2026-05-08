"""Branch and commit query operations."""

from __future__ import annotations

import logging

from gobby.worktrees.git._runner import GitRunner

logger = logging.getLogger(__name__)


def get_default_branch(runner: GitRunner) -> str:
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
        result = runner._run_git(
            ["symbolic-ref", "refs/remotes/origin/HEAD"],
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Output is like "refs/remotes/origin/main"
            ref = result.stdout.strip()
            if ref.startswith("refs/remotes/origin/"):
                branch = ref[len("refs/remotes/origin/") :]
                logger.debug(f"Detected default branch from origin/HEAD: {branch}")
                return branch
    except Exception as e:
        logger.debug(f"Method 1 (origin/HEAD) for default branch failed: {e}")

    # Method 2: Check which common default branches exist
    for branch in ["main", "master", "develop"]:
        try:
            # Check if the branch exists locally or remotely
            result = runner._run_git(
                ["rev-parse", "--verify", f"refs/heads/{branch}"],
                timeout=5,
            )
            if result.returncode == 0:
                logger.debug(f"Detected default branch from local ref: {branch}")
                return branch

            # Check remote
            result = runner._run_git(
                ["rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
                timeout=5,
            )
            if result.returncode == 0:
                logger.debug(f"Detected default branch from remote ref: {branch}")
                return branch
        except Exception as e:
            logger.debug(f"Method 2 branch check failed for {branch}: {e}")
            continue

    # Method 3: Fall back to "main"
    logger.debug("Could not detect default branch, falling back to 'main'")
    return "main"


def get_current_branch(runner: GitRunner) -> str | None:
    """
    Get the current branch of the repository.

    Returns:
        Branch name, or None if in detached HEAD state
    """
    try:
        result = runner._run_git(
            ["branch", "--show-current"],
            timeout=5,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return branch if branch else None
        return None
    except Exception:
        return None


def has_unpushed_commits(runner: GitRunner, branch: str | None = None) -> tuple[bool, int]:
    """
    Check if the branch has commits not pushed to origin.

    Args:
        branch: Branch to check (defaults to current branch)

    Returns:
        Tuple of (has_unpushed, count) where:
        - has_unpushed: True if there are unpushed commits
        - count: Number of unpushed commits (0 if none or error)
    """
    if branch is None:
        branch = get_current_branch(runner)
    if not branch:
        return False, 0

    try:
        # Check if remote tracking branch exists
        result = runner._run_git(
            ["rev-parse", "--verify", f"origin/{branch}"],
            timeout=5,
        )
        if result.returncode != 0:
            # No remote tracking branch - all local commits are "unpushed"
            # Count commits on the branch
            count_result = runner._run_git(
                ["rev-list", "--count", branch],
                timeout=5,
            )
            if count_result.returncode == 0:
                count = int(count_result.stdout.strip())
                return count > 0, count
            return True, 0

        # Count commits ahead of origin
        result = runner._run_git(
            ["rev-list", "--count", f"origin/{branch}..{branch}"],
            timeout=5,
        )
        if result.returncode == 0:
            count = int(result.stdout.strip())
            return count > 0, count
        return False, 0
    except Exception as e:
        logger.warning(f"Error checking unpushed commits: {e}")
        return False, 0


def get_local_commit(runner: GitRunner, branch: str) -> str | None:
    """
    Get the commit SHA of a local branch.

    Args:
        branch: Branch name

    Returns:
        Commit SHA, or None if branch doesn't exist
    """
    try:
        result = runner._run_git(
            ["rev-parse", branch],
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None
