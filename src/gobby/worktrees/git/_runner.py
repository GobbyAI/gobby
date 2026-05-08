"""Subprocess primitives shared by every worktree git operation."""

from __future__ import annotations

import logging
import subprocess  # nosec B404 # subprocess needed for git worktree operations
from pathlib import Path

logger = logging.getLogger(__name__)

_UNMERGED_ARGS = ["diff", "--name-only", "--diff-filter=U"]


class GitRunner:
    """
    Base class providing the `git` subprocess interface used by every
    `WorktreeGitManager` operation.

    Holds the repository path and exposes the low-level `_run_git` helper plus
    a few thin convenience wrappers (`run_git_command`, `stage_files`,
    `get_unmerged_files`) that are part of the public surface.
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
            result = subprocess.run(  # nosec B603 # cmd built from hardcoded git arguments
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

    def run_git_command(
        self,
        args: list[str],
        cwd: str | Path | None = None,
        timeout: int = 30,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return self._run_git(args, cwd=cwd, timeout=timeout, check=check)

    def stage_files(
        self, paths: list[str], *, cwd: str | Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.run_git_command(["add", "--", *paths], cwd=cwd, timeout=10)

    def get_unmerged_files(self, *, cwd: str | Path | None = None) -> list[str]:
        result = self.run_git_command(_UNMERGED_ARGS, cwd=cwd, timeout=10)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
            raise RuntimeError(f"failed to list unmerged files: {detail}")
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
