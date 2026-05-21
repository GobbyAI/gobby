"""Pre-spawn synchronization for reused agent worktrees."""

from __future__ import annotations

import asyncio
import logging
import subprocess  # nosec B404 # git subprocess results are mediated by WorktreeGitManager.
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReusedWorktreeSyncResult:
    """Summary of a reused worktree base sync."""

    status: str
    base_ref: str
    base_commit_sha: str


async def sync_reused_worktree_to_base(
    *,
    git_manager: Any,
    worktree_path: str,
    base_branch: str,
) -> ReusedWorktreeSyncResult:
    """Rebase a clean reused worktree onto the current local base branch."""

    return await asyncio.to_thread(
        _sync_reused_worktree_to_base_sync,
        git_manager,
        worktree_path,
        base_branch,
    )


def _sync_reused_worktree_to_base_sync(
    git_manager: Any,
    worktree_path: str,
    base_branch: str,
) -> ReusedWorktreeSyncResult:
    path = Path(worktree_path)
    if not path.is_dir():
        raise RuntimeError(f"Cannot sync reused worktree; path does not exist: {worktree_path}")

    base_ref, base_sha = _resolve_base_ref(git_manager, base_branch)
    _ensure_clean_worktree(git_manager, path)

    ancestor = _run_git(git_manager, ["merge-base", "--is-ancestor", base_ref, "HEAD"], cwd=path)
    if ancestor.returncode == 0:
        return ReusedWorktreeSyncResult("already_current", base_ref, base_sha)
    if ancestor.returncode not in {0, 1}:
        detail = _detail(ancestor)
        raise RuntimeError(f"Failed to compare reused worktree with {base_ref}: {detail}")

    result = _run_git(git_manager, ["rebase", base_ref], cwd=path, timeout=120)
    if result.returncode != 0:
        abort_detail = _abort_rebase(git_manager, path)
        detail = _detail(result)
        raise RuntimeError(
            f"Failed to rebase reused worktree onto {base_ref}: {detail}{abort_detail}"
        )

    logger.info("Rebased reused worktree %s onto %s", worktree_path, base_ref)
    return ReusedWorktreeSyncResult("rebased", base_ref, base_sha)


def _resolve_base_ref(git_manager: Any, base_branch: str) -> tuple[str, str]:
    local = _run_git(git_manager, ["rev-parse", "--verify", f"{base_branch}^{{commit}}"])
    if local.returncode == 0:
        return base_branch, local.stdout.strip()

    fetch = _run_git(git_manager, ["fetch", "origin", base_branch], timeout=60)
    if fetch.returncode != 0:
        detail = _detail(fetch) or _detail(local)
        raise RuntimeError(f"Failed to resolve base branch {base_branch}: {detail}")

    remote_ref = f"origin/{base_branch}"
    remote = _run_git(git_manager, ["rev-parse", "--verify", f"{remote_ref}^{{commit}}"])
    if remote.returncode != 0:
        detail = _detail(remote)
        raise RuntimeError(f"Failed to resolve base branch {base_branch}: {detail}")
    return remote_ref, remote.stdout.strip()


def _ensure_clean_worktree(git_manager: Any, path: Path) -> None:
    status = _run_git(git_manager, ["status", "--porcelain"], cwd=path)
    if status.returncode != 0:
        detail = _detail(status)
        raise RuntimeError(f"Failed to inspect reused worktree cleanliness: {detail}")
    if status.stdout.strip():
        raise RuntimeError(
            "Cannot reuse worktree with uncommitted changes; commit, stash, or inspect it first"
        )


def _abort_rebase(git_manager: Any, path: Path) -> str:
    abort = _run_git(git_manager, ["rebase", "--abort"], cwd=path, timeout=30)
    if abort.returncode == 0:
        return "; rebase aborted"
    return f"; rebase abort failed: {_detail(abort)}"


def _run_git(
    git_manager: Any,
    args: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return git_manager.run_git_command(args, cwd=cwd, timeout=timeout)


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or "unknown git error"
