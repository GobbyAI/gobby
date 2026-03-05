"""Git clone operations manager.

Provides operations for managing full git clones, distinct from worktrees.
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # nosec B404 - subprocess needed for git clone operations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


def _sanitize_url(url: str) -> str:
    """Remove credentials from URL for safe logging.

    Args:
        url: URL that may contain credentials

    Returns:
        URL with credentials removed
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    if parsed.username or parsed.password:
        # Replace userinfo with placeholder
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    return urlunparse(parsed)


@dataclass
class CloneStatus:
    """Status of a git clone including changes and sync state."""

    has_uncommitted_changes: bool
    has_staged_changes: bool
    has_untracked_files: bool
    branch: str | None
    commit: str | None


@dataclass
class GitOperationResult:
    """Result of a git operation."""

    success: bool
    message: str
    output: str | None = None
    error: str | None = None


class CloneGitManager:
    """
    Manager for git clone operations.

    Provides methods to shallow clone, sync, and delete git clones.
    Unlike worktrees which share a .git directory, clones are full
    repository copies suitable for isolated or cross-machine development.
    """

    def __init__(self, repo_path: str | Path) -> None:
        """
        Initialize with base repository path.

        Args:
            repo_path: Path to the reference repository (for getting remote URL)

        Raises:
            ValueError: If the repository path does not exist
        """
        self.repo_path = Path(repo_path)
        if not self.repo_path.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")

    def _run_git(
        self,
        args: list[str],
        cwd: str | Path | None = None,
        timeout: int = 60,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """
        Run a git command.

        Args:
            args: Git command arguments (without 'git' prefix)
            cwd: Working directory (defaults to repo_path)
            timeout: Command timeout in seconds
            check: Raise exception on non-zero exit

        Returns:
            CompletedProcess with stdout/stderr
        """
        if cwd is None:
            cwd = self.repo_path

        cmd = ["git"] + args
        logger.debug(f"Running: {' '.join(cmd)} in {cwd}")

        try:
            result = subprocess.run(  # nosec B603 - cmd built from hardcoded git arguments
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=check,
            )
            return result
        except subprocess.TimeoutExpired:
            logger.error(f"Git command timed out: {' '.join(cmd)}")
            raise
        except subprocess.CalledProcessError as e:
            logger.error(f"Git command failed: {' '.join(cmd)}, stderr: {e.stderr}")
            raise

    def get_remote_url(self, remote: str = "origin") -> str | None:
        """
        Get the remote URL for the repository.

        Args:
            remote: Remote name (default: origin)

        Returns:
            Remote URL or None if not found
        """
        try:
            result = self._run_git(
                ["remote", "get-url", remote],
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except (subprocess.SubprocessError, OSError) as e:
            logger.error(f"Failed to get remote URL for '{remote}': {e}", exc_info=True)
            return None

    def shallow_clone(
        self,
        remote_url: str,
        clone_path: str | Path,
        branch: str = "main",
        depth: int = 1,
    ) -> GitOperationResult:
        """
        Create a shallow clone of a repository.

        Args:
            remote_url: URL of the remote repository (HTTPS or SSH)
            clone_path: Path where clone will be created
            branch: Branch to clone
            depth: Clone depth (default: 1 for shallowest)

        Returns:
            GitOperationResult with success status and message
        """
        clone_path = Path(clone_path)

        # Check if path already exists
        if clone_path.exists():
            return GitOperationResult(
                success=False,
                message=f"Path already exists: {clone_path}",
            )

        # Ensure parent directory exists
        clone_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Build clone command
            cmd = [
                "git",
                "clone",
                "--depth",
                str(depth),
                "--single-branch",
                "-b",
                branch,
                remote_url,
                str(clone_path),
            ]

            # Sanitize URL in command before logging to avoid exposing credentials
            safe_cmd = cmd.copy()
            safe_cmd[safe_cmd.index(remote_url)] = _sanitize_url(remote_url)
            logger.debug(f"Running: {' '.join(safe_cmd)}")

            result = subprocess.run(  # nosec B603 - cmd built from hardcoded git arguments
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minutes for clone
            )

            if result.returncode == 0:
                return GitOperationResult(
                    success=True,
                    message=f"Successfully cloned to {clone_path}",
                    output=result.stdout,
                )
            else:
                return GitOperationResult(
                    success=False,
                    message=f"Clone failed: {result.stderr}",
                    error=result.stderr,
                )

        except subprocess.TimeoutExpired:
            # Clean up partial clone
            if clone_path.exists():
                shutil.rmtree(clone_path, ignore_errors=True)
            return GitOperationResult(
                success=False,
                message="Git clone timed out",
            )
        except (subprocess.SubprocessError, OSError) as e:
            # Clean up partial clone
            if clone_path.exists():
                shutil.rmtree(clone_path, ignore_errors=True)
            return GitOperationResult(
                success=False,
                message=f"Error cloning repository: {e}",
                error=str(e),
            )

    def full_clone(
        self,
        remote_url: str,
        clone_path: str | Path,
        branch: str = "main",
    ) -> GitOperationResult:
        """
        Create a full (non-shallow) clone of a repository.

        Args:
            remote_url: URL of the remote repository (HTTPS or SSH)
            clone_path: Path where clone will be created
            branch: Branch to clone

        Returns:
            GitOperationResult with success status and message
        """
        clone_path = Path(clone_path)

        # Check if path already exists
        if clone_path.exists():
            return GitOperationResult(
                success=False,
                message=f"Path already exists: {clone_path}",
            )

        # Ensure parent directory exists
        clone_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Build clone command without --depth (full clone)
            cmd = [
                "git",
                "clone",
                "-b",
                branch,
                remote_url,
                str(clone_path),
            ]

            # Sanitize URL in command before logging to avoid exposing credentials
            safe_cmd = cmd.copy()
            safe_cmd[safe_cmd.index(remote_url)] = _sanitize_url(remote_url)
            logger.debug(f"Running: {' '.join(safe_cmd)}")

            result = subprocess.run(  # nosec B603 - cmd built from hardcoded git arguments
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minutes for full clone
            )

            if result.returncode == 0:
                return GitOperationResult(
                    success=True,
                    message=f"Successfully cloned to {clone_path}",
                    output=result.stdout,
                )
            else:
                return GitOperationResult(
                    success=False,
                    message=f"Clone failed: {result.stderr}",
                    error=result.stderr,
                )

        except subprocess.TimeoutExpired:
            # Clean up partial clone
            if clone_path.exists():
                shutil.rmtree(clone_path, ignore_errors=True)
            return GitOperationResult(
                success=False,
                message="Git clone timed out",
            )
        except (subprocess.SubprocessError, OSError) as e:
            # Clean up partial clone
            if clone_path.exists():
                shutil.rmtree(clone_path, ignore_errors=True)
            return GitOperationResult(
                success=False,
                message=f"Error cloning repository: {e}",
                error=str(e),
            )

    def sync_clone(
        self,
        clone_path: str | Path,
        direction: Literal["pull", "push", "both"] = "pull",
        remote: str = "origin",
    ) -> GitOperationResult:
        """
        Sync a clone with its remote.

        Args:
            clone_path: Path to the clone directory
            direction: Sync direction ("pull", "push", or "both")
            remote: Remote name (default: origin)

        Returns:
            GitOperationResult with success status and message
        """
        clone_path = Path(clone_path)

        if not clone_path.exists():
            return GitOperationResult(
                success=False,
                message=f"Clone path does not exist: {clone_path}",
            )

        try:
            if direction in ("pull", "both"):
                # Pull changes
                pull_result = self._run_git(
                    ["pull", remote],
                    cwd=clone_path,
                    timeout=120,
                )
                if pull_result.returncode != 0:
                    return GitOperationResult(
                        success=False,
                        message=f"Pull failed: {pull_result.stderr or pull_result.stdout}",
                        error=pull_result.stderr or pull_result.stdout,
                    )

            if direction in ("push", "both"):
                # Push changes
                push_result = self._run_git(
                    ["push", remote],
                    cwd=clone_path,
                    timeout=120,
                )
                if push_result.returncode != 0:
                    return GitOperationResult(
                        success=False,
                        message=f"Push failed: {push_result.stderr}",
                        error=push_result.stderr,
                    )

            return GitOperationResult(
                success=True,
                message=f"Successfully synced ({direction}) with {remote}",
            )

        except subprocess.TimeoutExpired:
            return GitOperationResult(
                success=False,
                message="Git sync timed out",
            )
        except (subprocess.SubprocessError, OSError) as e:
            return GitOperationResult(
                success=False,
                message=f"Error syncing clone: {e}",
                error=str(e),
            )

    def delete_clone(
        self,
        clone_path: str | Path,
        force: bool = False,
    ) -> GitOperationResult:
        """
        Delete a clone directory.

        Args:
            clone_path: Path to the clone directory
            force: Force deletion even if there are uncommitted changes

        Returns:
            GitOperationResult with success status and message
        """
        clone_path = Path(clone_path)

        if not clone_path.exists():
            return GitOperationResult(
                success=True,
                message=f"Clone already does not exist: {clone_path}",
            )

        try:
            # Check for uncommitted changes unless force
            if not force:
                status = self.get_clone_status(clone_path)
                if status and status.has_uncommitted_changes:
                    return GitOperationResult(
                        success=False,
                        message="Clone has uncommitted changes. Use force=True to delete anyway.",
                    )

            # Remove the directory
            shutil.rmtree(clone_path)

            return GitOperationResult(
                success=True,
                message=f"Deleted clone at {clone_path}",
            )

        except (subprocess.SubprocessError, OSError) as e:
            return GitOperationResult(
                success=False,
                message=f"Error deleting clone: {e}",
                error=str(e),
            )

    def get_clone_status(
        self,
        clone_path: str | Path,
    ) -> CloneStatus | None:
        """
        Get status of a clone.

        Args:
            clone_path: Path to the clone directory

        Returns:
            CloneStatus or None if path is not valid
        """
        clone_path = Path(clone_path)

        if not clone_path.exists():
            return None

        try:
            # Get current branch
            branch_result = self._run_git(
                ["branch", "--show-current"],
                cwd=clone_path,
                timeout=5,
            )
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None

            # Get current commit
            commit_result = self._run_git(
                ["rev-parse", "--short", "HEAD"],
                cwd=clone_path,
                timeout=5,
            )
            commit = commit_result.stdout.strip() if commit_result.returncode == 0 else None

            # Get status (porcelain for parsing)
            status_result = self._run_git(
                ["status", "--porcelain"],
                cwd=clone_path,
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

            return CloneStatus(
                has_uncommitted_changes=has_uncommitted,
                has_staged_changes=has_staged,
                has_untracked_files=has_untracked,
                branch=branch,
                commit=commit,
            )

        except Exception as e:
            logger.error(f"Error getting clone status: {e}")
            return None

    def create_clone(
        self,
        clone_path: str | Path,
        branch_name: str,
        base_branch: str = "main",
        shallow: bool = True,
        use_local: bool = False,
    ) -> GitOperationResult:
        """
        Create a clone for isolated work.

        This is the high-level API used by CloneIsolationHandler.
        It gets the remote URL (or uses the local repo path) and creates
        either a shallow or full clone at the specified path.

        Args:
            clone_path: Path where clone will be created
            branch_name: Branch to create/checkout in the clone
            base_branch: Base branch to clone from (default: main)
            shallow: Whether to create a shallow clone (default: True)
            use_local: If True, clone from local repo path instead of remote URL.
                       Forces full clone (shallow from local paths can be unreliable).

        Returns:
            GitOperationResult with success status and message
        """
        if use_local:
            # Clone from local repo path — always full clone
            source = str(self.repo_path)
            logger.info(f"Cloning from local repo: {source}")
            result = self.full_clone(
                remote_url=source,
                clone_path=clone_path,
                branch=base_branch,
            )
        else:
            # Get remote URL from current repo
            remote_url = self.get_remote_url()
            if not remote_url:
                return GitOperationResult(
                    success=False,
                    message="Could not get remote URL from repository",
                    error="no_remote_url",
                )

            # Create clone (shallow or full based on parameter)
            if shallow:
                result = self.shallow_clone(
                    remote_url=remote_url,
                    clone_path=clone_path,
                    branch=base_branch,
                    depth=1,
                )
            else:
                result = self.full_clone(
                    remote_url=remote_url,
                    clone_path=clone_path,
                    branch=base_branch,
                )

        if not result.success:
            return result

        # If branch_name differs from base_branch, create and checkout the new branch
        if branch_name != base_branch:
            try:
                # Create new branch from base
                create_result = self._run_git(
                    ["checkout", "-b", branch_name],
                    cwd=clone_path,
                    timeout=30,
                )
                if create_result.returncode != 0:
                    # Clean up the clone on branch creation failure
                    try:
                        if Path(clone_path).exists():
                            shutil.rmtree(clone_path)
                    except Exception as cleanup_err:
                        logger.warning(
                            f"Failed to clean up clone after branch creation failure: {cleanup_err}"
                        )
                    return GitOperationResult(
                        success=False,
                        message=f"Failed to create branch {branch_name}: {create_result.stderr}",
                        error=create_result.stderr,
                    )
            except Exception as e:
                # Clean up the clone on exception
                try:
                    if Path(clone_path).exists():
                        shutil.rmtree(clone_path)
                except Exception as cleanup_err:
                    logger.warning(
                        f"Failed to clean up clone after branch creation error: {cleanup_err}"
                    )
                return GitOperationResult(
                    success=False,
                    message=f"Error creating branch: {e}",
                    error=str(e),
                )

        return GitOperationResult(
            success=True,
            message=f"Successfully created clone at {clone_path} on branch {branch_name}",
            output=result.output,
        )

    def merge_branch(
        self,
        source_branch: str,
        target_branch: str = "main",
        working_dir: str | Path | None = None,
    ) -> GitOperationResult:
        """
        Merge source branch into target branch.

        Performs:
        1. Fetch latest from remote
        2. Checkout target branch
        3. Attempt merge from source branch

        Args:
            source_branch: Branch to merge from
            target_branch: Branch to merge into (default: main)
            working_dir: Working directory (defaults to repo_path)

        Returns:
            GitOperationResult with success status and conflict info
        """
        cwd = Path(working_dir) if working_dir else self.repo_path

        if not cwd.exists():
            return GitOperationResult(
                success=False,
                message=f"Working directory does not exist: {cwd}",
                error="directory_not_found",
            )

        # Save current branch so we can restore on failure
        original_branch: str | None = None
        branch_result = self._run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            timeout=5,
        )
        if branch_result.returncode == 0:
            original_branch = branch_result.stdout.strip()

        checked_out_target = False

        try:
            # Fetch latest
            fetch_result = self._run_git(
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
            checkout_result = self._run_git(
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
            pull_result = self._run_git(
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

            # Attempt merge
            merge_result = self._run_git(
                ["merge", f"origin/{source_branch}", "--no-edit"],
                cwd=cwd,
                timeout=60,
            )

            if merge_result.returncode != 0:
                # Check if it's a conflict
                if "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr:
                    # Get list of conflicted files
                    status_result = self._run_git(
                        ["diff", "--name-only", "--diff-filter=U"],
                        cwd=cwd,
                        timeout=10,
                    )
                    conflicted_files = [f for f in status_result.stdout.strip().split("\n") if f]

                    # Abort the merge to leave repo in clean state
                    self._run_git(["merge", "--abort"], cwd=cwd, timeout=10)

                    return GitOperationResult(
                        success=False,
                        message=f"Merge conflict in {len(conflicted_files)} files",
                        error="merge_conflict",
                        output="\n".join(conflicted_files),
                    )

                # Non-conflict merge failure — abort to clean up
                self._run_git(["merge", "--abort"], cwd=cwd, timeout=10)

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
            # Attempt to abort any in-progress merge
            self._run_git(["merge", "--abort"], cwd=cwd, timeout=10)
            return GitOperationResult(
                success=False,
                message="Merge operation timed out",
                error="timeout",
            )
        except Exception as e:
            # Attempt to abort any in-progress merge
            self._run_git(["merge", "--abort"], cwd=cwd, timeout=10)
            return GitOperationResult(
                success=False,
                message=f"Merge error: {e}",
                error=str(e),
            )
        finally:
            # Restore original branch if we checked out the target
            if checked_out_target and original_branch and original_branch != target_branch:
                self._run_git(
                    ["checkout", original_branch],
                    cwd=cwd,
                    timeout=30,
                )
