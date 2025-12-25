"""
Git metadata extraction utilities for Gobby Client.

Provides functions to extract git repository information including:
- Repository remote URL
- Current branch name

Handles git worktrees, detached HEAD, and missing remotes gracefully.
"""

import logging
import subprocess
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)


class GitMetadata(TypedDict, total=False):
    """Git repository metadata structure."""

    github_url: str | None
    git_branch: str | None


def run_git_command(command: list[str], cwd: str | Path, timeout: int = 5) -> str | None:
    """
    Execute a git command safely with timeout protection.

    Args:
        command: Git command as list of strings (e.g., ["git", "branch", "--show-current"])
        cwd: Working directory where git command should run
        timeout: Command timeout in seconds (default: 5)

    Returns:
        Command output as string (stripped), or None if command fails
    """
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,  # Don't raise on non-zero exit
        )

        if result.returncode == 0:
            return result.stdout.strip()

        logger.debug(f"Git command failed: {' '.join(command)}, stderr: {result.stderr.strip()}")
        return None

    except subprocess.TimeoutExpired:
        logger.warning(f"Git command timed out after {timeout}s: {' '.join(command)}")
        return None
    except FileNotFoundError:
        logger.warning("Git executable not found in PATH")
        return None
    except Exception as e:
        logger.error(f"Git command error: {' '.join(command)}, error: {e}")
        return None


def get_github_url(cwd: str | Path) -> str | None:
    """
    Extract git repository URL from origin remote.

    Args:
        cwd: Working directory (git repository path)

    Returns:
        Remote URL string, or None if not available
    """
    # Try to get origin remote URL
    url = run_git_command(["git", "remote", "get-url", "origin"], cwd)

    if url:
        # Sanitize URL (remove auth tokens, convert SSH to HTTPS for privacy)
        # Keep original format for now - can sanitize later if needed
        return url

    # If origin doesn't exist, try to list all remotes and use first one
    remotes = run_git_command(["git", "remote"], cwd)
    if remotes:
        remote_names = remotes.split("\n")
        if remote_names:
            first_remote = remote_names[0]
            url = run_git_command(["git", "remote", "get-url", first_remote], cwd)
            if url:
                logger.debug(f"Using remote '{first_remote}' (origin not found)")
                return url

    logger.debug("No git remotes found")
    return None


def get_git_branch(cwd: str | Path) -> str | None:
    """
    Get current git branch name.

    Handles detached HEAD state gracefully.

    Args:
        cwd: Working directory (git repository path)

    Returns:
        Branch name string, or None if detached HEAD or error
    """
    branch = run_git_command(["git", "branch", "--show-current"], cwd)

    if branch:
        return branch

    # Check if we're in detached HEAD state
    symbolic_ref = run_git_command(["git", "symbolic-ref", "-q", "HEAD"], cwd)
    if symbolic_ref is None:
        logger.debug("Git repository in detached HEAD state")
        return None  # Detached HEAD

    logger.debug("Unable to determine current git branch")
    return None


def get_git_metadata(cwd: str | Path | None = None) -> GitMetadata:
    """
    Extract comprehensive git repository metadata.

    Extracts:
    - github_url: Remote repository URL (from origin or first remote)
    - git_branch: Current branch name (None if detached HEAD)

    Handles errors gracefully and works with git worktrees.

    Args:
        cwd: Working directory to check. Defaults to current directory.

    Returns:
        GitMetadata dict with available information.
        All fields are optional and will be None if unavailable.

    Example:
        >>> metadata = get_git_metadata("/path/to/repo")
        >>> metadata["github_url"]
        'https://github.com/user/repo.git'
        >>> metadata["git_branch"]
        'main'
    """
    if cwd is None:
        cwd = Path.cwd()
    else:
        cwd = Path(cwd)

    # Verify path exists
    if not cwd.exists():
        logger.warning(f"Path does not exist: {cwd}")
        return GitMetadata()

    # Check if directory is in a git repository
    is_git_repo = run_git_command(["git", "rev-parse", "--git-dir"], cwd)
    if not is_git_repo:
        logger.debug(f"Not a git repository: {cwd}")
        return GitMetadata()

    # Extract metadata
    metadata = GitMetadata()

    try:
        metadata["github_url"] = get_github_url(cwd)
        metadata["git_branch"] = get_git_branch(cwd)

        logger.debug(
            f"Git metadata extracted: repo={metadata.get('github_url')}, "
            f"branch={metadata.get('git_branch')}"
        )

    except Exception as e:
        logger.error(f"Error extracting git metadata: {e}")

    return metadata
