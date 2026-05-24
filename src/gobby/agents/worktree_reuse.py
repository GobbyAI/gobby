"""Pre-spawn synchronization for reused agent worktrees."""

from __future__ import annotations

import asyncio
import logging
import subprocess  # nosec B404 # git subprocess results are mediated by WorktreeGitManager.
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from gobby.agents.isolation_git_hygiene import (
    MCP_CONFIG_RELATIVE_PATH,
    PROJECT_JSON_RELATIVE_PATH,
    apply_isolation_git_hygiene,
    is_generated_isolation_project_json,
)

logger = logging.getLogger(__name__)


class ReusedWorktreeRebaseConflict(RuntimeError):
    """Raised when a clean reused worktree cannot be rebased without conflicts."""

    def __init__(
        self,
        message: str,
        *,
        worktree_path: str,
        base_ref: str,
        base_commit_sha: str,
    ) -> None:
        super().__init__(message)
        self.worktree_path = worktree_path
        self.base_ref = base_ref
        self.base_commit_sha = base_commit_sha


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
        message = f"Failed to rebase reused worktree onto {base_ref}: {detail}{abort_detail}"
        if _looks_like_rebase_conflict(detail):
            raise ReusedWorktreeRebaseConflict(
                message,
                worktree_path=worktree_path,
                base_ref=base_ref,
                base_commit_sha=base_sha,
            )
        raise RuntimeError(message)

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
    main_repo_path = _main_repo_path(git_manager)
    apply_isolation_git_hygiene(path, main_repo_path=main_repo_path)
    status = _run_git(git_manager, ["status", "--porcelain"], cwd=path)
    if status.returncode != 0:
        detail = _detail(status)
        raise RuntimeError(f"Failed to inspect reused worktree cleanliness: {detail}")
    blocking_lines = _blocking_status_lines(status.stdout, path, main_repo_path)
    if blocking_lines:
        raise RuntimeError(
            "Cannot reuse worktree with uncommitted changes; commit, stash, or inspect it first"
        )


def _abort_rebase(git_manager: Any, path: Path) -> str:
    abort = _run_git(git_manager, ["rebase", "--abort"], cwd=path, timeout=30)
    if abort.returncode == 0:
        return "; rebase aborted"
    return f"; rebase abort failed: {_detail(abort)}"


def _looks_like_rebase_conflict(detail: str) -> bool:
    normalized = detail.lower()
    return any(
        marker in normalized
        for marker in (
            "conflict",
            "could not apply",
            "fix conflicts and then run",
            "resolve all conflicts manually",
        )
    )


def _run_git(
    git_manager: Any,
    args: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return cast(
        subprocess.CompletedProcess[str],
        git_manager.run_git_command(args, cwd=cwd, timeout=timeout),
    )


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or "unknown git error"


def _main_repo_path(git_manager: Any) -> str | Path | None:
    repo_path = getattr(git_manager, "repo_path", None)
    return cast(str | Path | None, repo_path)


def _blocking_status_lines(
    status_output: str,
    worktree_path: Path,
    main_repo_path: str | Path | None,
) -> list[str]:
    blocking: list[str] = []
    for line in status_output.splitlines():
        relative_path = _porcelain_path(line)
        if relative_path == MCP_CONFIG_RELATIVE_PATH:
            continue
        if relative_path == PROJECT_JSON_RELATIVE_PATH and is_generated_isolation_project_json(
            worktree_path / PROJECT_JSON_RELATIVE_PATH,
            main_repo_path=main_repo_path,
        ):
            continue
        blocking.append(line)
    return blocking


def _porcelain_path(line: str) -> str:
    path = line[3:].strip() if len(line) > 3 else line.strip()
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[1]
    return path.strip('"')
