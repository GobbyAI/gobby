"""Subprocess primitives shared by every worktree git operation."""

from __future__ import annotations

import logging
import os
import subprocess  # nosec B404 # subprocess needed for git worktree operations
from collections.abc import Mapping
from pathlib import Path

from gobby.utils.daemon_git import GitTimeout, daemon_git

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

    async def _run_git(
        self,
        args: list[str],
        cwd: str | Path | None = None,
        timeout: int = 30,
        check: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """
        Run a git command.

        Args:
            args: Git command arguments (without 'git' prefix)
            cwd: Working directory (defaults to repo_path)
            timeout: Command timeout in seconds
            check: Raise exception on non-zero exit
            env: Environment variables to add or override

        Returns:
            CompletedProcess with stdout/stderr
        """
        if cwd is None:
            cwd = self.repo_path

        cmd = ["git", *args]
        logger.debug("Running: %s in %s", " ".join(cmd), cwd)

        effective_env = {**os.environ, **env} if env is not None else None
        outcome = await daemon_git.run(args, cwd=cwd, timeout=timeout, env=effective_env)
        if isinstance(outcome, GitTimeout):
            logger.error("Git command timed out: %s", " ".join(cmd))
            raise subprocess.TimeoutExpired(cmd, timeout, outcome.stdout, outcome.stderr)
        if outcome.returncode is None:
            logger.error("Git command unavailable: %s, stderr: %s", " ".join(cmd), outcome.stderr)
            raise RuntimeError(outcome.stderr or "git command unavailable")

        result = subprocess.CompletedProcess(
            cmd,
            outcome.returncode,
            outcome.stdout,
            outcome.stderr,
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
        timeout: int = 30,
        check: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return await self._run_git(args, cwd=cwd, timeout=timeout, check=check, env=env)

    async def stage_files(
        self, paths: list[str], *, cwd: str | Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return await self.run_git_command(["add", "--", *paths], cwd=cwd, timeout=10)

    async def get_unmerged_files(self, *, cwd: str | Path | None = None) -> list[str]:
        result = await self.run_git_command(_UNMERGED_ARGS, cwd=cwd, timeout=10)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
            raise RuntimeError(f"failed to list unmerged files: {detail}")
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
