"""
Git metadata extraction utilities for Gobby Client.

Provides functions to extract git repository information including:
- Repository remote URL
- Current branch name

Handles git worktrees, detached HEAD, and missing remotes gracefully.
"""

import logging
import os
import shutil
import subprocess  # nosec B404 # subprocess needed for git commands
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

GIT_FALLBACK_PATHS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


class GitMetadata(TypedDict, total=False):
    """Git repository metadata structure."""

    github_url: str | None
    git_branch: str | None


def git_subprocess_env() -> dict[str, str] | None:
    """Return a PATH-augmented environment only when git is not currently resolvable."""
    if shutil.which("git") is not None:
        return None

    env = os.environ.copy()
    path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    for part in GIT_FALLBACK_PATHS:
        if part not in path_parts:
            path_parts.append(part)
    env["PATH"] = os.pathsep.join(path_parts)
    return env


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
        env = git_subprocess_env()
        if env is None:
            result = subprocess.run(  # nosec B603 # internal git command
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,  # Don't raise on non-zero exit
            )
        else:
            result = subprocess.run(  # nosec B603 # internal git command
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,  # Don't raise on non-zero exit
                env=env,
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


def _resolve_git_directory(cwd: str | Path | None) -> Path | None:
    candidate = Path.cwd() if cwd is None else Path(cwd).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        logger.warning("Git metadata path does not exist")
        return None
    if not resolved.is_dir():
        logger.warning("Git metadata path is not a directory")
        return None
    return resolved


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
    cwd = _resolve_git_directory(cwd)
    if cwd is None:
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


def normalize_commit_sha(sha: str | None, cwd: str | Path | None = None) -> str | None:
    """
    Normalize a commit SHA to dynamic short format.

    Verifies the object exists and is a commit (not a blob, tree, or tag),
    then uses git rev-parse --short to return the minimum characters
    needed for uniqueness (typically 7, more in large repos).

    Args:
        sha: Short or full commit SHA
        cwd: Working directory for git commands (defaults to current directory)

    Returns:
        Shortened SHA (7+ chars), or None if SHA cannot be resolved
        or does not refer to a commit object
    """
    if not sha or len(sha) < 4:
        return None

    if cwd is None:
        cwd = Path.cwd()

    # Verify object exists and is a commit (not blob/tree/tag)
    obj_type = run_git_command(["git", "cat-file", "-t", sha], cwd=cwd)
    if obj_type != "commit":
        return None

    # Normalize to canonical short form
    result = run_git_command(["git", "rev-parse", "--short", sha], cwd=cwd)
    return result if result else None


def is_valid_sha_format(sha: str) -> bool:
    """
    Check if string looks like a valid SHA format (hex, >= 4 chars).

    This is a format check only - does not verify the SHA exists in any repo.

    Args:
        sha: String to validate

    Returns:
        True if string could be a valid SHA format
    """
    if not sha or len(sha) < 4:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in sha)
