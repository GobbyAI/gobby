"""Pre-spawn synchronization for reused agent worktrees."""

from __future__ import annotations

import asyncio
import logging
import subprocess  # nosec B404 # git subprocess results are mediated by WorktreeGitManager.
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

from gobby.agents.cargo_target import cleanup_checkout_cargo_target_dir
from gobby.agents.isolation_git_hygiene import apply_isolation_git_hygiene, blocking_status_lines
from gobby.utils.project_context import IsolationProjectJsonError, ensure_project_json_for_isolation
from gobby.worktrees.base_branch import select_worktree_base
from gobby.worktrees.deletion import probe_missing_worktree_git_state

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeferredBaseSync:
    """A base merge left to the seat because the tree holds WIP or its own commits."""

    reason: Literal["uncommitted_changes", "branch_diverged"]
    pending_base_sha: str


@dataclass(frozen=True)
class ReusedWorktreeSyncResult:
    """Summary of a reused worktree base sync.

    ``base_commit_sha`` is the base the tree actually contains. A deferred sync
    reports the tree's merge-base and carries the unmerged target in ``deferred``.
    """

    status: Literal["already_current", "merged", "deferred"]
    base_ref: str
    base_commit_sha: str
    deferred: DeferredBaseSync | None = None

    def isolation_extra(self) -> dict[str, Any]:
        """Isolation-context fields for the spawn banner and response."""
        return {"deferred_base_sync": asdict(self.deferred)} if self.deferred else {}


def deferred_base_sync_notice(extra: dict[str, Any]) -> str:
    """Banner lines telling a reused-worktree seat who finishes the deferred base merge."""
    deferred = extra.get("deferred_base_sync")
    if not isinstance(deferred, dict):
        return ""
    pending = deferred.get("pending_base_sha")
    base = extra.get("base_branch") or "the base branch"
    cause = (
        "it holds uncommitted work"
        if deferred.get("reason") == "uncommitted_changes"
        else "its branch carries commits that are not on the base"
    )
    return (
        "\n"
        f"Base sync deferred: this worktree was handed over as-is because {cause}.\n"
        "If the branch is yours, inspect `git status` and the branch log, commit any work "
        f"in progress, then run `git merge {pending}` ({base}) and resolve conflicts before "
        "you land. A review-only seat leaves the branch as it is.\n"
    )


async def sync_reused_worktree_to_base(
    *,
    git_manager: Any,
    worktree_path: str,
    base_branch: str | None,
    base_commit_sha: str | None = None,
) -> ReusedWorktreeSyncResult:
    """Merge the current local base into a reused worktree, preserving checkpoint SHAs.

    Reuse never refuses for work in progress: a dirty tree skips the merge, and a
    conflicting merge is aborted. Both return ``deferred`` with the tree untouched.
    """

    return await _sync_reused_worktree_to_base(
        git_manager,
        worktree_path,
        base_branch,
        base_commit_sha,
    )


async def cleanup_stale_worktree_registration(
    git_manager: Any,
    worktree_storage: Any,
    worktree: Any,
) -> None:
    git_already_deleted, retry_error = await probe_missing_worktree_git_state(
        git_manager,
        worktree_path=worktree.worktree_path,
        branch_name=worktree.branch_name,
    )
    if retry_error is not None:
        raise RuntimeError(retry_error)
    if not git_already_deleted:
        delete_result = await git_manager.delete_worktree(
            worktree_path=worktree.worktree_path,
            force=True,
            delete_branch=True,
            branch_name=worktree.branch_name,
            base_branch=worktree.base_branch,
        )
        if not delete_result.success:
            raise RuntimeError(f"Failed to clean up stale worktree: {delete_result.error}")
        if delete_result.error and "not found" not in delete_result.error:
            raise RuntimeError(f"Failed to delete stale worktree branch: {delete_result.error}")
    cargo_error = await asyncio.to_thread(
        cleanup_checkout_cargo_target_dir,
        Path(worktree.worktree_path),
        worktree.project_id,
    )
    if cargo_error is not None:
        raise RuntimeError(
            "cargo_target_cleanup_failed: Git worktree files were deleted, but Cargo target "
            f"cleanup failed: {cargo_error}"
        )
    worktree_storage.delete(worktree.id)


async def _sync_reused_worktree_to_base(
    git_manager: Any,
    worktree_path: str,
    base_branch: str | None,
    base_commit_sha: str | None = None,
) -> ReusedWorktreeSyncResult:
    path = Path(worktree_path)
    if not path.is_dir():
        raise RuntimeError(f"Cannot sync reused worktree; path does not exist: {worktree_path}")

    if base_commit_sha is None:
        base_ref, base_sha = await _resolve_base_ref(git_manager, base_branch)
    else:
        base_ref, base_sha = base_branch or "HEAD", base_commit_sha
    clean = await _is_clean_worktree(git_manager, path)

    ancestor = await _run_git(
        git_manager, ["merge-base", "--is-ancestor", base_sha, "HEAD"], cwd=path
    )
    if ancestor.returncode == 0:
        return ReusedWorktreeSyncResult("already_current", base_ref, base_sha)
    if ancestor.returncode not in {0, 1}:
        detail = _detail(ancestor)
        raise RuntimeError(f"Failed to compare reused worktree with {base_ref}: {detail}")
    if not clean:
        # Never merge onto uncommitted work; the seat commits it, then merges.
        return await _deferred_sync(git_manager, path, base_ref, base_sha, "uncommitted_changes")

    behind = await _run_git(
        git_manager, ["merge-base", "--is-ancestor", "HEAD", base_sha], cwd=path
    )
    if behind.returncode == 1:
        # Any merge here would write a commit the branch's author never made.
        return await _deferred_sync(git_manager, path, base_ref, base_sha, "branch_diverged")
    if behind.returncode != 0:
        detail = _detail(behind)
        raise RuntimeError(f"Failed to compare reused worktree with {base_ref}: {detail}")

    result = await _run_git(git_manager, ["merge", "--ff-only", base_sha], cwd=path, timeout=120)
    if result.returncode != 0:
        detail = _detail(result)
        raise RuntimeError(f"Failed to fast-forward reused worktree to {base_ref}: {detail}")

    logger.info("Fast-forwarded reused worktree %s to %s", worktree_path, base_ref)
    return ReusedWorktreeSyncResult("merged", base_ref, base_sha)


async def _resolve_base_ref(git_manager: Any, base_branch: str | None) -> tuple[str, str]:
    branch, commit = await select_worktree_base(git_manager, base_branch)
    remote = base_branch is not None and base_branch.startswith(("origin/", "refs/remotes/origin/"))
    ref = f"refs/remotes/origin/{branch}" if remote else f"refs/heads/{branch}"
    return ref, commit


async def _deferred_sync(
    git_manager: Any,
    path: Path,
    base_ref: str,
    base_sha: str,
    reason: Literal["uncommitted_changes", "branch_diverged"],
) -> ReusedWorktreeSyncResult:
    fork = await _run_git(git_manager, ["merge-base", "HEAD", base_sha], cwd=path)
    fork_sha = fork.stdout.strip()
    if fork.returncode != 0 or not fork_sha:
        raise RuntimeError(
            f"Failed to find reused worktree merge-base with {base_ref}: {_detail(fork)}"
        )
    logger.info("Deferred %s merge into reused worktree %s (%s)", base_ref, path, reason)
    return ReusedWorktreeSyncResult(
        "deferred",
        base_ref,
        fork_sha,
        DeferredBaseSync(reason=reason, pending_base_sha=base_sha),
    )


async def _is_clean_worktree(git_manager: Any, path: Path) -> bool:
    main_repo_path = _main_repo_path(git_manager)
    if main_repo_path is not None:
        try:
            await ensure_project_json_for_isolation(main_repo_path, path)
        except IsolationProjectJsonError as exc:
            raise RuntimeError(
                f"Failed to write isolation sidecar for reused worktree {path}: {exc}"
            ) from exc
    await apply_isolation_git_hygiene(path, main_repo_path=main_repo_path)
    status = await _run_git(git_manager, ["status", "--porcelain"], cwd=path)
    if status.returncode != 0:
        detail = _detail(status)
        raise RuntimeError(f"Failed to inspect reused worktree cleanliness: {detail}")
    return not blocking_status_lines(status.stdout, path, main_repo_path)


async def _run_git(
    git_manager: Any,
    args: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return cast(
        subprocess.CompletedProcess[str],
        await git_manager.run_git_command(args, cwd=cwd, timeout=timeout),
    )


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or "unknown git error"


def _main_repo_path(git_manager: Any) -> str | Path | None:
    repo_path = getattr(git_manager, "repo_path", None)
    return cast(str | Path | None, repo_path)
