"""Git clone operations manager.

Provides operations for managing full git clones, distinct from worktrees.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess  # nosec B404 # subprocess needed for git clone operations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gobby.app_context import get_app_context
from gobby.utils.daemon_git import GitOk, GitTimeout, daemon_git
from gobby.utils.url_sanitize import sanitize_url
from gobby.worktrees.git import WorktreeGitManager
from gobby.worktrees.occupancy import refuse_occupied_worktree

logger = logging.getLogger(__name__)
CLONES_ROOT = Path.home() / ".gobby" / "clones"


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

    async def _run_git(
        self,
        args: list[str],
        cwd: str | Path | None = None,
        timeout: int = 60,
        check: bool = False,
        env: dict[str, str] | None = None,
        display_args: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """
        Run a git command.

        Args:
            args: Git command arguments (without 'git' prefix)
            cwd: Working directory (defaults to repo_path)
            timeout: Command timeout in seconds
            check: Raise exception on non-zero exit
            env: Extra environment variables to set (merged with os.environ)

        Returns:
            CompletedProcess with stdout/stderr
        """
        if cwd is None:
            cwd = self.repo_path

        cmd = ["git", *args]
        display_cmd = ["git", *(display_args if display_args is not None else args)]
        logger.debug("Running: %s in %s", " ".join(display_cmd), cwd)

        run_env = None
        if env:
            run_env = {**os.environ, **env}

        outcome = await daemon_git.run(
            args,
            cwd=cwd,
            timeout=timeout,
            env=run_env,
        )
        if isinstance(outcome, GitTimeout):
            logger.error("Git command timed out: %s", " ".join(cmd))
            raise subprocess.TimeoutExpired(
                cmd,
                outcome.timeout,
                output=outcome.stdout,
                stderr=outcome.stderr,
            )
        if outcome.returncode is None:
            logger.error("Git command unavailable: %s, stderr: %s", " ".join(cmd), outcome.stderr)
            raise OSError(outcome.stderr or "Git could not be started")
        result = subprocess.CompletedProcess(
            args=cmd,
            returncode=0 if isinstance(outcome, GitOk) else outcome.returncode,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
        )
        if check and result.returncode != 0:
            logger.error("Git command failed: %s, stderr: %s", " ".join(cmd), result.stderr)
            raise subprocess.CalledProcessError(
                result.returncode,
                cmd,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result

    async def run_git_command(
        self,
        args: list[str],
        cwd: str | Path | None = None,
        timeout: int = 60,
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command in a clone and return the completed process.

        Args:
            args: Git subcommand and arguments, excluding the leading ``git``.
            cwd: Working directory for the command. Defaults to this clone's
                repository path when omitted.
            timeout: Maximum runtime in seconds. Defaults to 60.
            check: When true, non-zero exit codes raise
                ``subprocess.CalledProcessError``.
            env: Optional environment overrides merged with ``os.environ``.

        Returns:
            ``subprocess.CompletedProcess[str]`` with text stdout/stderr.

        Raises:
            subprocess.SubprocessError: For timeout and non-zero exit failures
                surfaced by ``subprocess.run`` when ``check=True``.

        Notes:
            Delegates to ``_run_git``, which logs timed-out commands and failed
            checked commands before re-raising.
        """
        return await self._run_git(args, cwd=cwd, timeout=timeout, check=check, env=env)

    def resolve_managed_clone_path(self, clone_path: str | Path) -> Path | None:
        """Canonicalize a clone path and enforce containment under the managed root."""
        resolved_path = Path(clone_path).expanduser().resolve()
        clones_root = CLONES_ROOT.expanduser().resolve()

        if resolved_path == clones_root or not resolved_path.is_relative_to(clones_root):
            return None
        return resolved_path

    async def get_remote_url(
        self,
        remote: str = "origin",
        cwd: str | Path | None = None,
    ) -> str | None:
        """
        Get the remote URL for the repository.

        Args:
            remote: Remote name (default: origin)

        Returns:
            Remote URL or None if not found
        """
        try:
            if cwd is None:
                result = await self._run_git(["remote", "get-url", remote], timeout=10)
            else:
                result = await self._run_git(
                    ["remote", "get-url", remote],
                    cwd=cwd,
                    timeout=10,
                )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except (subprocess.SubprocessError, OSError) as e:
            logger.exception("Failed to get remote URL for '%s': %s", remote, e)
            return None

    async def get_default_branch(self) -> str:
        """Detect the default branch of the parent repository."""
        return await WorktreeGitManager(self.repo_path).get_default_branch()

    async def shallow_clone(
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
        resolved_clone_path = self.resolve_managed_clone_path(clone_path)
        if resolved_clone_path is None:
            return GitOperationResult(
                success=False,
                message=f"Clone path must be under {CLONES_ROOT.expanduser()}",
                error="clone_path_outside_root",
            )
        clone_path = resolved_clone_path

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
            args = [
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
            safe_args = args.copy()
            safe_args[safe_args.index(remote_url)] = sanitize_url(remote_url)
            result = await self._run_git(
                args,
                cwd=self.repo_path,
                timeout=300,
                display_args=safe_args,
            )

            if result.returncode == 0:
                # Set pull.rebase so divergent branches are handled cleanly
                await self._run_git(["config", "pull.rebase", "true"], cwd=clone_path)
                return GitOperationResult(
                    success=True,
                    message=f"Successfully cloned to {clone_path}",
                    output=result.stdout,
                )
            else:
                if clone_path.exists():
                    await asyncio.to_thread(shutil.rmtree, clone_path, ignore_errors=True)
                return GitOperationResult(
                    success=False,
                    message=f"Clone failed: {result.stderr}",
                    error=result.stderr,
                )

        except asyncio.CancelledError:
            if clone_path.exists():
                await asyncio.to_thread(shutil.rmtree, clone_path, ignore_errors=True)
            raise
        except subprocess.TimeoutExpired:
            # Clean up partial clone
            if clone_path.exists():
                await asyncio.to_thread(shutil.rmtree, clone_path, ignore_errors=True)
            return GitOperationResult(
                success=False,
                message="Git clone timed out",
            )
        except (subprocess.SubprocessError, OSError) as e:
            # Clean up partial clone
            if clone_path.exists():
                await asyncio.to_thread(shutil.rmtree, clone_path, ignore_errors=True)
            return GitOperationResult(
                success=False,
                message=f"Error cloning repository: {e}",
                error=str(e),
            )

    async def full_clone(
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
        resolved_clone_path = self.resolve_managed_clone_path(clone_path)
        if resolved_clone_path is None:
            return GitOperationResult(
                success=False,
                message=f"Clone path must be under {CLONES_ROOT.expanduser()}",
                error="clone_path_outside_root",
            )
        clone_path = resolved_clone_path

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
            args = [
                "clone",
                "-b",
                branch,
                remote_url,
                str(clone_path),
            ]

            # Sanitize URL in command before logging to avoid exposing credentials
            safe_args = args.copy()
            safe_args[safe_args.index(remote_url)] = sanitize_url(remote_url)
            result = await self._run_git(
                args,
                cwd=self.repo_path,
                timeout=600,
                display_args=safe_args,
            )

            if result.returncode == 0:
                # Set pull.rebase so divergent branches are handled cleanly
                await self._run_git(["config", "pull.rebase", "true"], cwd=clone_path)
                return GitOperationResult(
                    success=True,
                    message=f"Successfully cloned to {clone_path}",
                    output=result.stdout,
                )
            else:
                if clone_path.exists():
                    await asyncio.to_thread(shutil.rmtree, clone_path, ignore_errors=True)
                return GitOperationResult(
                    success=False,
                    message=f"Clone failed: {result.stderr}",
                    error=result.stderr,
                )

        except asyncio.CancelledError:
            if clone_path.exists():
                await asyncio.to_thread(shutil.rmtree, clone_path, ignore_errors=True)
            raise
        except subprocess.TimeoutExpired:
            # Clean up partial clone
            if clone_path.exists():
                await asyncio.to_thread(shutil.rmtree, clone_path, ignore_errors=True)
            return GitOperationResult(
                success=False,
                message="Git clone timed out",
            )
        except (subprocess.SubprocessError, OSError) as e:
            # Clean up partial clone
            if clone_path.exists():
                await asyncio.to_thread(shutil.rmtree, clone_path, ignore_errors=True)
            return GitOperationResult(
                success=False,
                message=f"Error cloning repository: {e}",
                error=str(e),
            )

    async def sync_clone(
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
        clone_path = Path(clone_path).expanduser()

        if not clone_path.exists():
            return GitOperationResult(
                success=False,
                message=f"Clone path does not exist: {clone_path}",
            )

        try:
            if direction in ("pull", "both"):
                # Pull with rebase to handle divergent branches cleanly
                pull_result = await self._run_git(
                    ["pull", "--rebase", remote],
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
                push_result = await self._run_git(
                    ["push", remote],
                    cwd=clone_path,
                    timeout=120,
                )
                if push_result.returncode != 0:
                    detail = push_result.stderr or push_result.stdout
                    return GitOperationResult(
                        success=False,
                        message=f"Push failed: {detail}",
                        error=detail,
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

    async def delete_clone(
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
        resolved_clone_path = self.resolve_managed_clone_path(clone_path)
        if resolved_clone_path is None:
            clones_root = CLONES_ROOT.expanduser().resolve()
            return GitOperationResult(
                success=False,
                message=f"Refusing to delete path outside clones root: {clone_path}",
                error=f"clone_path_not_under_{clones_root}",
            )

        clone_path = resolved_clone_path

        if not clone_path.exists():
            return GitOperationResult(
                success=True,
                message=f"Clone already does not exist: {clone_path}",
            )

        # Every caller runs in the daemon; without its container there is no hub to ask.
        services = get_app_context()
        if services is not None:
            occupied = await asyncio.to_thread(
                refuse_occupied_worktree, services.database, str(clone_path)
            )
            if occupied is not None:
                return GitOperationResult(success=False, message=occupied, error=occupied)

        try:
            # Check for uncommitted changes unless force
            if not force:
                status = await self.get_clone_status(clone_path)
                if status is None or (status.branch is None and status.commit is None):
                    return GitOperationResult(
                        success=False,
                        message="Refusing to delete path because it is not a valid git clone.",
                        error="invalid_clone_path",
                    )
                if status.has_uncommitted_changes:
                    return GitOperationResult(
                        success=False,
                        message="Clone has uncommitted changes. Use force=True to delete anyway.",
                    )

            # Remove the directory
            await asyncio.to_thread(shutil.rmtree, clone_path)

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

    async def get_clone_status(
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
        clone_path = Path(clone_path).expanduser()

        if not clone_path.exists():
            return None

        try:
            # Get current branch
            branch_result = await self._run_git(
                ["branch", "--show-current"],
                cwd=clone_path,
                timeout=5,
            )
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
            branch = branch or None

            # Get current commit
            commit_result = await self._run_git(
                ["rev-parse", "--short", "HEAD"],
                cwd=clone_path,
                timeout=5,
            )
            commit = commit_result.stdout.strip() if commit_result.returncode == 0 else None
            commit = commit or None

            # Get status (porcelain for parsing)
            status_result = await self._run_git(
                ["status", "--porcelain"],
                cwd=clone_path,
                timeout=10,
            )
            if status_result.returncode != 0:
                return None

            has_staged = False
            has_uncommitted = False
            has_untracked = False

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

        except (subprocess.SubprocessError, OSError) as e:
            logger.error("Error getting clone status: %s", e)
            return None

    async def _clone_selected_commit(
        self, clone_path: str | Path, branch_name: str, commit_sha: str
    ) -> GitOperationResult:
        """Clone the local repository and start branch_name at the selected commit.

        A local-path clone copies the source object store, so a commit reachable
        only from an unpushed branch or a remote-tracking ref needs no fetch.
        """
        resolved_clone_path = self.resolve_managed_clone_path(clone_path)
        if resolved_clone_path is None:
            return GitOperationResult(
                success=False,
                message=f"Clone path must be under {CLONES_ROOT.expanduser()}",
                error="clone_path_outside_root",
            )
        if resolved_clone_path.exists():
            return GitOperationResult(
                success=False,
                message=f"Path already exists: {resolved_clone_path}",
            )
        resolved_clone_path.parent.mkdir(parents=True, exist_ok=True)
        steps = [
            (["clone", "--no-checkout", str(self.repo_path), str(resolved_clone_path)], None),
            (["checkout", "-B", branch_name, commit_sha], resolved_clone_path),
            (["config", "pull.rebase", "true"], resolved_clone_path),
        ]
        try:
            for args, cwd in steps:
                result = await self._run_git(args, cwd=cwd or self.repo_path, timeout=600)
                if result.returncode != 0:
                    await asyncio.to_thread(shutil.rmtree, resolved_clone_path, ignore_errors=True)
                    return GitOperationResult(
                        success=False,
                        message=f"git {args[0]} failed: {result.stderr}",
                        error=result.stderr,
                    )
        except asyncio.CancelledError:
            await asyncio.to_thread(shutil.rmtree, resolved_clone_path, ignore_errors=True)
            raise
        except (subprocess.SubprocessError, OSError) as e:
            await asyncio.to_thread(shutil.rmtree, resolved_clone_path, ignore_errors=True)
            return GitOperationResult(
                success=False,
                message=f"Error cloning repository: {e}",
                error=str(e),
            )
        return GitOperationResult(
            success=True,
            message=f"Cloned {commit_sha} to {resolved_clone_path} on branch {branch_name}",
        )

    async def create_clone(
        self,
        clone_path: str | Path,
        branch_name: str,
        base_branch: str = "main",
        shallow: bool = True,
        use_local: bool = False,
        base_commit_sha: str | None = None,
    ) -> GitOperationResult:
        """
        Create a clone for isolated work.

        This is the high-level API used by CloneIsolationHandler.
        With base_commit_sha it clones the local repository at that selected
        commit; otherwise it gets the remote URL (or uses the local repo path)
        and creates either a shallow or full clone at the specified path.

        Args:
            clone_path: Path where clone will be created
            branch_name: Branch to create/checkout in the clone
            base_branch: Base branch to clone from (default: main)
            shallow: Whether to create a shallow clone (default: True)
            use_local: If True, clone from local repo path instead of remote URL.
                       Forces full clone (shallow from local paths can be unreliable).
            base_commit_sha: Selected fork commit; branch_name starts exactly here.

        Returns:
            GitOperationResult with success status and message
        """
        if base_commit_sha is not None:
            return await self._clone_selected_commit(clone_path, branch_name, base_commit_sha)
        if use_local:
            # Clone from local repo path — always full clone
            source = str(self.repo_path)
            logger.info("Cloning from local repo: %s", source)
            result = await self.full_clone(
                remote_url=source,
                clone_path=clone_path,
                branch=base_branch,
            )
        else:
            # Get remote URL from current repo
            remote_url = await self.get_remote_url()
            if not remote_url:
                return GitOperationResult(
                    success=False,
                    message="Could not get remote URL from repository",
                    error="no_remote_url",
                )

            # Create clone (shallow or full based on parameter)
            if shallow:
                result = await self.shallow_clone(
                    remote_url=remote_url,
                    clone_path=clone_path,
                    branch=base_branch,
                    depth=1,
                )
            else:
                result = await self.full_clone(
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
                create_result = await self._run_git(
                    ["checkout", "-b", branch_name],
                    cwd=clone_path,
                    timeout=30,
                )
                if create_result.returncode != 0:
                    # Clean up the clone on branch creation failure
                    try:
                        if Path(clone_path).exists():
                            await asyncio.to_thread(shutil.rmtree, clone_path)
                    except Exception as cleanup_err:
                        logger.warning(
                            "Failed to clean up clone after branch creation failure: %s",
                            cleanup_err,
                        )
                    return GitOperationResult(
                        success=False,
                        message=f"Failed to create branch {branch_name}: {create_result.stderr}",
                        error=create_result.stderr,
                    )
            except asyncio.CancelledError:
                if Path(clone_path).exists():
                    await asyncio.to_thread(shutil.rmtree, clone_path, ignore_errors=True)
                raise
            except Exception as e:
                # Clean up the clone on exception
                try:
                    if Path(clone_path).exists():
                        await asyncio.to_thread(shutil.rmtree, clone_path)
                except Exception as cleanup_err:
                    logger.warning(
                        "Failed to clean up clone after branch creation error: %s", cleanup_err
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

    async def merge_branch(
        self,
        source_branch: str,
        target_branch: str = "main",
        working_dir: str | Path | None = None,
        source_is_local: bool = False,
    ) -> GitOperationResult:
        """Merge source branch into target branch; see ``merge_clone_branch``."""
        from gobby.clones.merge import merge_clone_branch

        return await merge_clone_branch(
            self, source_branch, target_branch, working_dir, source_is_local
        )
