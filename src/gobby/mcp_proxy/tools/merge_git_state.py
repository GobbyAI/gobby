"""Git state helpers for merge MCP tools."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Protocol


class GitRunnerProtocol(Protocol):
    def run_git_command(
        self,
        args: list[str],
        cwd: str | Path | None = None,
        timeout: int = 30,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...


def _returncode(result: subprocess.CompletedProcess[str]) -> int | None:
    return result.returncode if isinstance(result.returncode, int) else None


async def current_branch(git_manager: GitRunnerProtocol, cwd: str | Path) -> str | None:
    result = await asyncio.to_thread(
        git_manager.run_git_command,
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd,
        timeout=10,
    )
    if _returncode(result) != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


async def merge_head_exists(git_manager: GitRunnerProtocol | None, cwd: str | Path) -> bool:
    if git_manager is None:
        return False
    result = await asyncio.to_thread(
        git_manager.run_git_command,
        ["rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=cwd,
        timeout=10,
    )
    rc = _returncode(result)
    return True if rc is None else rc == 0


async def rev_parse_head(git_manager: GitRunnerProtocol | None, cwd: str | Path) -> str | None:
    if git_manager is None:
        return None
    result = await asyncio.to_thread(
        git_manager.run_git_command,
        ["rev-parse", "HEAD"],
        cwd=cwd,
        timeout=10,
    )
    if _returncode(result) != 0:
        return None
    head = result.stdout.strip()
    return head or None


async def is_ancestor(
    git_manager: GitRunnerProtocol,
    cwd: str | Path,
    *,
    ancestor: str,
    descendant: str,
) -> bool:
    result = await asyncio.to_thread(
        git_manager.run_git_command,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        cwd=cwd,
        timeout=10,
    )
    rc = _returncode(result)
    return True if rc is None else rc == 0


async def source_branch_validation_error(
    *,
    git_manager: GitRunnerProtocol | None,
    worktree_path: str | Path,
    worktree_branch: str | None,
    source_branch: str,
    target_branch: str,
) -> str | None:
    """Return an actionable error when merge_start args do not match the worktree."""
    if source_branch == target_branch:
        return (
            "source_branch and target_branch must differ. For worktree conflict "
            "resolution, source_branch must be the worktree branch and target_branch "
            "must be the local branch being merged into."
        )
    if git_manager is None:
        return None

    current = await current_branch(git_manager, worktree_path)
    expected = worktree_branch if isinstance(worktree_branch, str) and worktree_branch else current
    if expected and source_branch != expected:
        return (
            f"source_branch '{source_branch}' does not match worktree branch "
            f"'{expected}'. Use source_branch='{expected}' and target_branch="
            f"'{target_branch}'."
        )
    return None


async def resolved_reuse_error(
    *,
    git_manager: GitRunnerProtocol | None,
    worktree_path: str | Path,
    target_branch: str,
) -> str | None:
    """Return why a resolved merge row is stale for the current worktree state."""
    if git_manager is None:
        return None
    if await merge_head_exists(git_manager, worktree_path):
        return None
    if await is_ancestor(
        git_manager,
        worktree_path,
        ancestor=target_branch,
        descendant="HEAD",
    ):
        return None
    return (
        "resolved merge state is stale: the worktree has no MERGE_HEAD and "
        f"HEAD does not contain target_branch '{target_branch}'"
    )
