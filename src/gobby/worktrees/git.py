"""Git worktree operations manager."""

from __future__ import annotations

import logging
import subprocess  # nosec B404 - subprocess needed for git worktree operations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class WorktreeInfo:
    """Information about a git worktree."""

    path: str
    branch: str | None
    commit: str
    is_bare: bool = False
    is_detached: bool = False
    locked: bool = False
    prunable: bool = False


@dataclass
class WorktreeStatus:
    """Status of a worktree including changes and sync state."""

    has_uncommitted_changes: bool
    has_staged_changes: bool
    has_untracked_files: bool
    ahead: int  # Commits ahead of upstream
    behind: int  # Commits behind upstream
    branch: str | None
    commit: str | None


@dataclass
class GitOperationResult:
    """Result of a git operation."""

    success: bool
    message: str
    output: str | None = None
    error: str | None = None


class WorktreeGitManager:
    """
    Manager for git worktree operations.

    Provides methods to create, delete, and manage git worktrees.
    All operations are performed relative to a base repository path.
    """

    def __init__(self, repo_path: str | Path):
        """
        Initialize with base repository path.

        Args:
            repo_path: Path to the main git repository
        """
        self.repo_path = Path(repo_path)
        if not self.repo_path.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")

    def _run_git(
        self,
        args: list[str],
        cwd: str | Path | None = None,
        timeout: int = 30,
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

    def create_worktree(
        self,
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
                    verify_result = self._run_git(["rev-parse", "--verify", base_branch], timeout=5)
                    if verify_result.returncode != 0:
                        return GitOperationResult(
                            success=False,
                            message=f"Local branch '{base_branch}' not found",
                            error=verify_result.stderr,
                        )

                    # Create worktree with new branch based on local ref
                    result = self._run_git(
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
                    fetch_result = self._run_git(["fetch", "origin", base_branch], timeout=60)
                    if fetch_result.returncode != 0:
                        return GitOperationResult(
                            success=False,
                            message=f"Failed to fetch origin/{base_branch}: {fetch_result.stderr}",
                            error=fetch_result.stderr,
                        )

                    # Create worktree with new branch
                    result = self._run_git(
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
                result = self._run_git(
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

    def delete_worktree(
        self,
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
                status = self.get_worktree_status(worktree_path)
                if status:
                    branch_name = status.branch
                if not branch_name:
                    logger.warning(
                        "Branch deletion skipped: branch_name could not be resolved "
                        "from get_worktree_status for worktree '%s'",
                        worktree_path,
                        extra={"worktree_path": str(worktree_path), "delete_branch": True},
                    )

            # Remove worktree
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(str(worktree_path))

            result = self._run_git(args, timeout=30)

            if result.returncode != 0:
                # git worktree remove --force only handles modified tracked files,
                # not untracked files (.mypy_cache, .ruff_cache, __pycache__, etc).
                # Fall back to manual removal + prune when force is requested.
                if force and worktree_path.exists():
                    import shutil

                    shutil.rmtree(worktree_path, ignore_errors=True)
                    self._run_git(["worktree", "prune"], timeout=10)
                    if not worktree_path.exists():
                        logger.info(
                            "Removed worktree via fallback (rmtree + prune): %s",
                            worktree_path,
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
                branch_result = self._run_git(
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
        self,
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
            fetch_result = self._run_git(
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
                sync_result = self._run_git(
                    ["rebase", f"origin/{base_branch}"],
                    cwd=worktree_path,
                    timeout=120,
                )
            else:
                sync_result = self._run_git(
                    ["merge", f"origin/{base_branch}", "--no-edit"],
                    cwd=worktree_path,
                    timeout=120,
                )

            if sync_result.returncode != 0:
                # Check if there are conflicts
                if "CONFLICT" in sync_result.stdout or "CONFLICT" in sync_result.stderr:
                    return GitOperationResult(
                        success=False,
                        message=f"Sync failed due to conflicts. Run 'git {strategy} --abort' to cancel.",
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

    def get_worktree_status(
        self,
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
            branch_result = self._run_git(
                ["branch", "--show-current"],
                cwd=worktree_path,
                timeout=5,
            )
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None

            # Get current commit
            commit_result = self._run_git(
                ["rev-parse", "--short", "HEAD"],
                cwd=worktree_path,
                timeout=5,
            )
            commit = commit_result.stdout.strip() if commit_result.returncode == 0 else None

            # Get status (porcelain for parsing)
            status_result = self._run_git(
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
                upstream_result = self._run_git(
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

    def list_worktrees(self) -> list[WorktreeInfo]:
        """
        List all worktrees for this repository.

        Returns:
            List of WorktreeInfo objects
        """
        try:
            result = self._run_git(
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

    def prune_worktrees(self) -> GitOperationResult:
        """
        Prune stale worktree entries.

        Returns:
            GitOperationResult with success status
        """
        try:
            result = self._run_git(["worktree", "prune"], timeout=30)

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

    def lock_worktree(
        self,
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
            result = self._run_git(args, timeout=10)

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

    def unlock_worktree(self, worktree_path: str | Path) -> GitOperationResult:
        """
        Unlock a worktree.

        Args:
            worktree_path: Path to the worktree

        Returns:
            GitOperationResult with success status
        """
        try:
            result = self._run_git(
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

    def get_default_branch(self) -> str:
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
            result = self._run_git(
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
            logger.debug("Method 1 (origin/HEAD) for default branch failed: %s", e)

        # Method 2: Check which common default branches exist
        for branch in ["main", "master", "develop"]:
            try:
                # Check if the branch exists locally or remotely
                result = self._run_git(
                    ["rev-parse", "--verify", f"refs/heads/{branch}"],
                    timeout=5,
                )
                if result.returncode == 0:
                    logger.debug(f"Detected default branch from local ref: {branch}")
                    return branch

                # Check remote
                result = self._run_git(
                    ["rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
                    timeout=5,
                )
                if result.returncode == 0:
                    logger.debug(f"Detected default branch from remote ref: {branch}")
                    return branch
            except Exception as e:
                logger.debug("Method 2 branch check failed for %s: %s", branch, e)
                continue

        # Method 3: Fall back to "main"
        logger.debug("Could not detect default branch, falling back to 'main'")
        return "main"

    def get_current_branch(self) -> str | None:
        """
        Get the current branch of the repository.

        Returns:
            Branch name, or None if in detached HEAD state
        """
        try:
            result = self._run_git(
                ["branch", "--show-current"],
                timeout=5,
            )
            if result.returncode == 0:
                branch = result.stdout.strip()
                return branch if branch else None
            return None
        except Exception:
            return None

    def has_unpushed_commits(self, branch: str | None = None) -> tuple[bool, int]:
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
            branch = self.get_current_branch()
        if not branch:
            return False, 0

        try:
            # Check if remote tracking branch exists
            result = self._run_git(
                ["rev-parse", "--verify", f"origin/{branch}"],
                timeout=5,
            )
            if result.returncode != 0:
                # No remote tracking branch - all local commits are "unpushed"
                # Count commits on the branch
                count_result = self._run_git(
                    ["rev-list", "--count", branch],
                    timeout=5,
                )
                if count_result.returncode == 0:
                    count = int(count_result.stdout.strip())
                    return count > 0, count
                return True, 0

            # Count commits ahead of origin
            result = self._run_git(
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

    def merge_branch(
        self,
        source_branch: str,
        target_branch: str = "main",
        push: bool = True,
    ) -> GitOperationResult:
        """
        Merge source branch into target branch in the main repository.

        Performs:
        1. Fetch latest from remote
        2. Checkout target branch
        3. Pull latest on target
        4. Attempt merge from source branch (--no-ff)
        5. Optionally push to remote

        On conflict: aborts merge, restores original branch.

        Args:
            source_branch: Branch to merge from
            target_branch: Branch to merge into (default: main)
            push: Whether to push after merge (default: True)

        Returns:
            GitOperationResult with success status and conflict info
        """
        # Save current branch for restoration
        original_branch: str | None = None
        branch_result = self._run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            timeout=5,
        )
        if branch_result.returncode == 0:
            original_branch = branch_result.stdout.strip()

        checked_out_target = False

        try:
            # Fetch latest
            fetch_result = self._run_git(
                ["fetch", "origin"],
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
                timeout=60,
            )
            if pull_result.returncode != 0:
                return GitOperationResult(
                    success=False,
                    message=f"Failed to pull {target_branch}: {pull_result.stderr}",
                    error=pull_result.stderr,
                )

            # Attempt merge with --no-ff
            merge_result = self._run_git(
                ["merge", source_branch, "--no-ff", "--no-edit"],
                timeout=60,
            )

            if merge_result.returncode != 0:
                # Check for conflicts
                if "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr:
                    # Get conflicted files
                    status_result = self._run_git(
                        ["diff", "--name-only", "--diff-filter=U"],
                        timeout=10,
                    )
                    conflicted_files = [f for f in status_result.stdout.strip().split("\n") if f]

                    # Abort merge
                    self._run_git(["merge", "--abort"], timeout=10)

                    return GitOperationResult(
                        success=False,
                        message=f"Merge conflict in {len(conflicted_files)} files",
                        error="merge_conflict",
                        output="\n".join(conflicted_files),
                    )

                # Non-conflict failure — abort
                self._run_git(["merge", "--abort"], timeout=10)

                return GitOperationResult(
                    success=False,
                    message=f"Merge failed: {merge_result.stderr}",
                    error=merge_result.stderr,
                )

            # Optionally push
            if push:
                push_result = self._run_git(
                    ["push", "origin", target_branch],
                    timeout=60,
                )
                if push_result.returncode != 0:
                    return GitOperationResult(
                        success=True,
                        message=f"Merged {source_branch} into {target_branch} but push failed: {push_result.stderr}",
                        error=push_result.stderr,
                        output=merge_result.stdout,
                    )

            return GitOperationResult(
                success=True,
                message=f"Successfully merged {source_branch} into {target_branch}",
                output=merge_result.stdout,
            )

        except subprocess.TimeoutExpired:
            self._run_git(["merge", "--abort"], timeout=10)
            return GitOperationResult(
                success=False,
                message="Merge operation timed out",
                error="timeout",
            )
        except Exception as e:
            self._run_git(["merge", "--abort"], timeout=10)
            return GitOperationResult(
                success=False,
                message=f"Merge error: {e}",
                error=str(e),
            )
        finally:
            # Restore original branch if we checked out target
            if checked_out_target and original_branch and original_branch != target_branch:
                self._run_git(
                    ["checkout", original_branch],
                    timeout=30,
                )

    def get_local_commit(self, branch: str) -> str | None:
        """
        Get the commit SHA of a local branch.

        Args:
            branch: Branch name

        Returns:
            Commit SHA, or None if branch doesn't exist
        """
        try:
            result = self._run_git(
                ["rev-parse", branch],
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except Exception:
            return None
