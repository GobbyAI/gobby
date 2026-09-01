"""
Git metadata extraction utilities for Gobby Client.

Provides functions to extract git repository information including:
- Repository remote URL
- Current branch name

Handles git worktrees, detached HEAD, and missing remotes gracefully.
"""

import asyncio
import logging
import os
import shutil
import subprocess  # nosec B404 # subprocess needed for git commands
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import TypedDict
from uuid import uuid4
from weakref import WeakKeyDictionary

logger = logging.getLogger(__name__)

GIT_FALLBACK_PATHS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
GIT_OPTIONAL_LOCKS_ENV = "GIT_OPTIONAL_LOCKS"
_CHECKOUT_MUTATION_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    WeakKeyDictionary()
)


def get_checkout_mutation_lock(checkout_path: str | Path) -> asyncio.Lock:
    """Return the current event loop's lock for mutations of a checkout."""
    loop = asyncio.get_running_loop()
    loop_locks = _CHECKOUT_MUTATION_LOCKS.setdefault(loop, {})
    key = str(Path(checkout_path).resolve())
    return loop_locks.setdefault(key, asyncio.Lock())


def stash_ref_for_oid(stash_list: str, stash_oid: str) -> str | None:
    """Resolve the current reflog selector for an exact stash object."""
    for line in stash_list.splitlines():
        stash_ref, separator, candidate_oid = line.partition("\0")
        if separator and candidate_oid == stash_oid:
            return stash_ref
    return None


def new_stash_marker(operation: str) -> str:
    """Return a collision-resistant marker for one operation-owned stash."""
    return f"gobby-{operation}:{uuid4().hex}"


def stash_oid_for_marker(stash_list: str, marker: str) -> str | None:
    """Resolve the stash object whose reflog subject ends with an exact marker."""
    for line in stash_list.splitlines():
        stash_oid, separator, subject = line.partition("\0")
        if separator and subject.endswith(marker):
            return stash_oid
    return None


async def run_to_completion[T](
    awaitable: Awaitable[T],
    *,
    on_cancel: Callable[[], None] | None = None,
) -> T:
    """Keep work alive through caller cancellation, then propagate cancellation.

    ``on_cancel`` lets a shielded transaction observe the cancellation request at
    its commit boundary. Work that has not started mutating shared state can stop;
    work past that boundary continues through cleanup before cancellation escapes.
    """
    worker = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        if worker.done() and worker.cancelled():
            raise

        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                if worker.done() and worker.cancelled():
                    raise

        if not worker.cancelled():
            try:
                worker.result()
            except BaseException as error:
                logger.warning(
                    "Offloaded work failed after its caller was cancelled: %s",
                    error,
                    exc_info=True,
                )
        raise


async def run_thread_to_completion[**P, T](
    func: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run blocking work without abandoning its thread when the caller is cancelled.

    ``asyncio.to_thread`` cannot stop its worker after cancellation. Callers that
    protect external state with an asyncio lock must therefore wait for the worker
    to finish before releasing that lock, while still propagating cancellation.
    """
    return await run_to_completion(asyncio.to_thread(func, *args, **kwargs))


class GitMetadata(TypedDict, total=False):
    """Git repository metadata structure."""

    github_url: str | None
    git_branch: str | None


def disable_optional_git_locks(environ: MutableMapping[str, str] | None = None) -> None:
    """Make every git subprocess of this process skip optional index locks.

    Read-only commands such as ``git status`` and ``git diff`` opportunistically
    refresh the index by writing ``.git/index.lock`` and renaming it over the
    index. A daemon read killed on timeout mid-refresh leaves that lock behind,
    and every later git write in the shared checkout fails with
    ``index.lock: File exists`` until someone removes it by hand (#21055).
    ``GIT_OPTIONAL_LOCKS=0`` skips the refresh write — the setting IDE git
    integrations run with — while the required locks of real writes are
    unaffected. Child processes inherit it.
    """
    target = os.environ if environ is None else environ
    target[GIT_OPTIONAL_LOCKS_ENV] = "0"


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

        logger.debug("Git command failed: %s, stderr: %s", " ".join(command), result.stderr.strip())
        return None

    except subprocess.TimeoutExpired:
        logger.warning("Git command timed out after %ss: %s", timeout, " ".join(command))
        return None
    except FileNotFoundError:
        # subprocess raises the same error for a missing executable and a missing cwd.
        if not Path(cwd).is_dir():
            logger.warning("Git working directory does not exist: %s", cwd)
        else:
            logger.warning("Git executable not found in PATH")
        return None
    except Exception as e:
        logger.exception("Git command error: %s, error: %s", " ".join(command), e)
        return None


def is_path_gitignored(path: str, cwd: str | Path) -> bool:
    """Return True only when git definitively reports the path as ignored.

    ``git check-ignore -q`` exits 0 for ignored paths; misses and errors
    (including non-git directories) are treated as not ignored so callers
    gating commit requirements stay conservative.
    """
    return run_git_command(["git", "check-ignore", "-q", "--", path], cwd=cwd) is not None


def _resolve_git_directory(cwd: str | Path | None) -> Path | None:
    candidate = Path.cwd() if cwd is None else Path(cwd).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        logger.warning("Git metadata path does not exist or cannot be resolved: %s", e)
        return None
    try:
        is_directory = resolved.is_dir()
    except OSError as e:
        logger.warning("Git metadata path cannot be inspected: %s", e)
        return None
    if not is_directory:
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
                logger.debug("Using remote '%s' (origin not found)", first_remote)
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
        logger.debug("Not a git repository: %s", cwd)
        return GitMetadata()

    # Extract metadata
    metadata = GitMetadata()

    try:
        metadata["github_url"] = get_github_url(cwd)
        metadata["git_branch"] = get_git_branch(cwd)

        logger.debug(
            "Git metadata extracted: repo=%s, branch=%s",
            metadata.get("github_url"),
            metadata.get("git_branch"),
        )

    except Exception as e:
        logger.exception("Error extracting git metadata: %s", e)

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
