"""Worktree CRUD: create, delete, sync."""

from __future__ import annotations

import logging
import shutil
import subprocess  # nosec B404 # subprocess.TimeoutExpired re-raised from runner
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from gobby.worktrees.git._models import GitOperationResult, WorktreeInfo
from gobby.worktrees.git._runner import GitRunner
from gobby.worktrees.git._status import get_worktree_status, list_worktrees

logger = logging.getLogger(__name__)


def inspect_linked_worktree(runner: GitRunner, worktree_path: str | Path) -> WorktreeInfo:
    """Inspect an existing linked worktree and return canonical Git metadata."""
    try:
        canonical_path = Path(worktree_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Worktree path does not exist: {worktree_path}") from exc

    if not canonical_path.is_dir():
        raise ValueError(f"Worktree path is not a directory: {canonical_path}")

    registered = list_worktrees(runner)
    if not registered:
        raise ValueError(f"Unable to inspect linked worktrees for {runner.repo_path}")

    primary_path = Path(registered[0].path).expanduser().resolve()
    if canonical_path == primary_path:
        raise ValueError(f"Primary checkout cannot be adopted: {canonical_path}")

    for worktree in registered[1:]:
        candidate_path = Path(worktree.path).expanduser().resolve()
        if candidate_path != canonical_path:
            continue
        if worktree.is_bare:
            raise ValueError(f"Bare worktree cannot be adopted: {canonical_path}")
        if worktree.prunable:
            raise ValueError(f"Prunable worktree cannot be adopted: {canonical_path}")
        return WorktreeInfo(
            path=str(canonical_path),
            branch=worktree.branch,
            commit=worktree.commit,
            is_bare=worktree.is_bare,
            is_detached=worktree.is_detached,
            locked=worktree.locked,
            prunable=worktree.prunable,
        )

    raise ValueError(f"Path is not a linked worktree: {canonical_path}")


def create_worktree(
    runner: GitRunner,
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
    if base_branch.startswith(("origin/", "refs/remotes/")):
        return GitOperationResult(
            success=False,
            message=f"Remote-style base branch is not allowed: {base_branch}",
            error="remote_base_branch_not_allowed",
        )
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
                verify_result = runner._run_git(["rev-parse", "--verify", base_branch], timeout=5)
                if verify_result.returncode != 0:
                    return GitOperationResult(
                        success=False,
                        message=f"Local branch '{base_branch}' not found",
                        error=verify_result.stderr,
                    )

                # Create worktree with new branch based on local ref
                result = runner._run_git(
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
                fetch_result = runner._run_git(["fetch", "origin", base_branch], timeout=60)
                if fetch_result.returncode != 0:
                    return GitOperationResult(
                        success=False,
                        message=f"Failed to fetch origin/{base_branch}: {fetch_result.stderr}",
                        error=fetch_result.stderr,
                    )

                # Create worktree with new branch
                result = runner._run_git(
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
            result = runner._run_git(
                ["worktree", "add", str(worktree_path), branch_name],
                timeout=60,
            )

        if create_branch and result.returncode != 0 and _is_branch_exists_error(result.stderr):
            logger.info(
                "Branch %s already exists while creating worktree; reusing existing branch",
                branch_name,
            )
            result = runner._run_git(
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


def _is_branch_exists_error(stderr: str | None) -> bool:
    if not stderr:
        return False
    normalized = stderr.lower()
    return "branch" in normalized and "already exists" in normalized


def _refuse_unmerged_branch_deletion(
    runner: GitRunner,
    branch_name: str,
    base_branch: str | None,
) -> GitOperationResult | None:
    """Refuse ordinary branch deletion unless the branch is merged into its base.

    Ordinary deletion must prove refs/heads/<branch> is an ancestor of the
    stored base. Fully qualified local refs only: a pushed-but-unmerged branch
    or a stale remote ref must never authorize deleting local commits, and
    git's own `-d` heuristic (merged into HEAD or upstream) checks the wrong
    target entirely.
    """
    if base_branch is None:
        return GitOperationResult(
            success=False,
            message=(
                f"Refusing to delete branch '{branch_name}': no base_branch was "
                "provided for the merge preflight. Pass the worktree's stored "
                "base branch, or force_delete_branch=True to deliberately "
                "abandon the branch."
            ),
            error="branch_deletion_requires_base_branch",
        )
    if base_branch.startswith(("origin/", "refs/remotes/")):
        return GitOperationResult(
            success=False,
            message=(
                f"Refusing to delete branch '{branch_name}': remote-style base branch "
                f"'{base_branch}' cannot prove local merge state."
            ),
            error="remote_base_branch_not_allowed",
        )
    source_ref = f"refs/heads/{branch_name}"
    target_ref = base_branch if base_branch.startswith("refs/") else f"refs/heads/{base_branch}"
    result = runner._run_git(
        ["merge-base", "--is-ancestor", source_ref, target_ref],
        timeout=10,
    )
    if result.returncode == 0:
        return None
    if result.returncode == 1:
        return GitOperationResult(
            success=False,
            message=(
                f"Refusing to delete branch '{branch_name}': it is not merged "
                f"into '{base_branch}'. Merge it first, or pass "
                "force_delete_branch=True to deliberately abandon its commits."
            ),
            error="branch_not_merged_into_base",
        )
    detail = result.stderr.strip() or result.stdout.strip()
    return GitOperationResult(
        success=False,
        message=(
            f"Refusing to delete branch '{branch_name}': merge state against "
            f"'{base_branch}' could not be verified: {detail}"
        ),
        error=detail or "merge_state_unresolvable",
    )


def delete_worktree(
    runner: GitRunner,
    worktree_path: str | Path,
    force: bool = False,
    delete_branch: bool = False,
    force_delete_branch: bool = False,
    branch_name: str | None = None,
    base_branch: str | None = None,
) -> GitOperationResult:
    """
    Delete a git worktree.

    Args:
        worktree_path: Path to the worktree to delete
        force: Force removal even if dirty (never implies branch force)
        delete_branch: Also delete the associated branch
        force_delete_branch: Force-delete the branch even if it is unmerged
        branch_name: Optional explicit branch name (if not provided, attempts to discover)
        base_branch: Stored base branch the task branch must be merged into;
            required for ordinary (non-forced) branch deletion

    Returns:
        GitOperationResult with success status and message
    """
    worktree_path = Path(worktree_path)

    try:
        # Get branch name before removal (for optional branch deletion)
        if delete_branch and not branch_name:
            status = get_worktree_status(runner, worktree_path)
            if status:
                branch_name = status.branch
            if not branch_name:
                logger.warning(
                    "Branch deletion skipped: branch_name could not be resolved from get_worktree_status for worktree '%s'",
                    worktree_path,
                    extra={"worktree_path": str(worktree_path), "delete_branch": True},
                )

        # Preflight before anything is removed: refusing here keeps the
        # directory, the branch, and the caller's DB record fully intact.
        if delete_branch and branch_name and not force_delete_branch:
            refusal = _refuse_unmerged_branch_deletion(runner, branch_name, base_branch)
            if refusal is not None:
                return refusal

        # Remove worktree
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree_path))

        path_existed_before_remove = worktree_path.exists()
        # `git worktree remove` unlinks the tree file by file; a worktree that
        # carries a build directory (cargo target/, node_modules) needs minutes,
        # and a timeout here kills git mid-deletion, leaving a prunable stub
        # (#21058). The MCP caller's budget for delete_worktree is 300s.
        result = runner._run_git(args, timeout=240)
        output = result.stdout
        remove_warning = ""

        if result.returncode != 0:
            if not path_existed_before_remove:
                prune_result = runner._run_git(["worktree", "prune"], timeout=10)
                output = prune_result.stdout
                detail = result.stderr.strip() or result.stdout.strip()
                remove_warning = f"; git remove reported: {detail}" if detail else ""
                if prune_result.returncode != 0:
                    prune_detail = prune_result.stderr.strip() or prune_result.stdout.strip()
                    return GitOperationResult(
                        success=False,
                        message=f"Failed to prune missing worktree: {prune_detail}",
                        error=prune_detail,
                    )
            elif path_existed_before_remove and not worktree_path.exists():
                prune_result = runner._run_git(["worktree", "prune"], timeout=10)
                output = prune_result.stdout
                detail = result.stderr.strip() or result.stdout.strip()
                remove_warning = f"; git remove reported: {detail}" if detail else ""
                if prune_result.returncode != 0:
                    prune_detail = prune_result.stderr.strip() or prune_result.stdout.strip()
                    remove_warning += f"; prune reported: {prune_detail}"
            # git worktree remove --force only handles modified tracked files,
            # not untracked files (.mypy_cache, .ruff_cache, __pycache__, etc).
            # Fall back to manual removal + prune when force is requested.
            elif force and worktree_path.exists():
                logger.warning(
                    "git worktree remove failed for %s; falling back to shutil.rmtree + worktree prune. stderr=%s",
                    worktree_path,
                    result.stderr.strip(),
                )
                shutil.rmtree(worktree_path, ignore_errors=True)
                prune_result = runner._run_git(["worktree", "prune"], timeout=10)
                output = prune_result.stdout
                if not worktree_path.exists():
                    logger.info("Removed worktree via fallback (rmtree + prune): %s", worktree_path)
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

        # Optionally delete the branch. The ordinary path already passed the
        # stored-base preflight above, so -D is used unconditionally here:
        # git's own -d heuristic checks merged-into-HEAD/upstream, which is
        # both weaker (upstream can be a pushed-but-unmerged remote ref) and
        # wrong-target (HEAD is whatever the main checkout happens to be on).
        if delete_branch and branch_name:
            branch_result = runner._run_git(
                ["branch", "-D", branch_name],
                timeout=10,
            )

            if branch_result.returncode != 0:
                return GitOperationResult(
                    success=False,
                    message=f"Worktree removed, but failed to delete branch: {branch_result.stderr}",
                    error=branch_result.stderr,
                )

        return GitOperationResult(
            success=True,
            message=f"Deleted worktree at {worktree_path}"
            + (f" and branch {branch_name}" if delete_branch and branch_name else "")
            + remove_warning,
            output=output,
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
    runner: GitRunner,
    worktree_path: str | Path,
    base_branch: str = "main",
    strategy: Literal["rebase", "merge"] = "rebase",
    source_branch: str | None = None,
    env: Mapping[str, str] | None = None,
) -> GitOperationResult:
    """
    Sync worktree with base branch.

    Args:
        worktree_path: Path to the worktree
        base_branch: Branch to sync from when source_branch is not provided
        strategy: Sync strategy (rebase or merge)
        source_branch: Explicit source branch/ref to sync from. Defaults to base_branch.
        env: Environment variables to add or override for the sync command.

    Returns:
        GitOperationResult with success status and message
    """
    worktree_path = Path(worktree_path)

    if not worktree_path.exists():
        return GitOperationResult(
            success=False,
            message=f"Worktree path does not exist: {worktree_path}",
        )

    def abort_sync() -> tuple[bool, str]:
        try:
            abort_result = runner._run_git(
                [strategy, "--abort"],
                cwd=worktree_path,
                timeout=30,
            )
            abort_error = abort_result.stderr.strip() or abort_result.stdout.strip()
            return abort_result.returncode == 0, abort_error
        except Exception as e:
            return False, str(e)

    try:
        sync_source = source_branch or base_branch
        if sync_source.startswith("origin/"):
            remote_branch = sync_source.removeprefix("origin/")
            fetch_result = runner._run_git(
                ["fetch", "origin", remote_branch],
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
            sync_result = runner._run_git(
                ["rebase", sync_source],
                cwd=worktree_path,
                timeout=120,
                env=env,
            )
        else:
            sync_result = runner._run_git(
                ["merge", sync_source, "--no-edit"],
                cwd=worktree_path,
                timeout=120,
                env=env,
            )

        if sync_result.returncode != 0:
            has_conflicts = "CONFLICT" in sync_result.stdout or "CONFLICT" in sync_result.stderr
            conflicted_files: list[str] = []
            if has_conflicts:
                conflict_result = runner._run_git(
                    ["diff", "--name-only", "--diff-filter=U"],
                    cwd=worktree_path,
                    timeout=10,
                )
                if conflict_result.returncode == 0:
                    conflicted_files = [
                        path.strip() for path in conflict_result.stdout.splitlines() if path.strip()
                    ]
            aborted, abort_error = abort_sync()
            abort_detail = "; aborted" if aborted else f"; abort failed: {abort_error}"

            # Check if there are conflicts
            if has_conflicts:
                return GitOperationResult(
                    success=False,
                    message=f"Sync failed due to conflicts{abort_detail}",
                    output="\n".join(conflicted_files),
                    error=sync_result.stderr or sync_result.stdout,
                )
            return GitOperationResult(
                success=False,
                message=f"Failed to {strategy}: {sync_result.stderr}{abort_detail}",
                error=sync_result.stderr,
            )

        return GitOperationResult(
            success=True,
            message=f"Successfully synced with {sync_source} using {strategy}",
            output=sync_result.stdout,
        )

    except subprocess.TimeoutExpired:
        aborted, abort_error = abort_sync()
        abort_detail = "; aborted" if aborted else f"; abort failed: {abort_error}"
        return GitOperationResult(
            success=False,
            message=f"Git command timed out{abort_detail}",
        )
    except Exception as e:
        return GitOperationResult(
            success=False,
            message=f"Error syncing worktree: {e}",
            error=str(e),
        )
