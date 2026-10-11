"""
Internal MCP tools for surveying the merge landscape across worktrees.

These tools answer "what's
unmerged, what will conflict, what state is each worktree in," and they
provide the surgical primitives (cherry-pick, subset merge, post-merge
verification) needed to drive a merge campaign.

All tools are registered onto the existing `gobby-merge` registry via
`register_merge_landscape_tools` rather than creating a new server.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import psycopg

from gobby.failure_categories import FailureCategory, classify_failure
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_verification import (
    normalize_verification_command,
    reject_verification_command,
    verification_environment,
)
from gobby.utils import spawn

if TYPE_CHECKING:
    from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)


class WorktreeManagerProtocol(Protocol):
    def get(self, worktree_id: str) -> Any | None: ...

    def resolve_reference(self, ref: str) -> str: ...

    def list_worktrees(
        self,
        *,
        project_id: str | None = None,
        status: str | tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> list[Any]: ...


class MergeStorageProtocol(Protocol):
    def get_active_resolution(self, worktree_id: str | None = None) -> Any | None: ...

    def get_latest_resolution(self, worktree_id: str) -> Any | None: ...

    def list_conflicts(
        self,
        resolution_id: str | None = None,
        file_path: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Any]: ...

    def create_conflict(
        self,
        resolution_id: str,
        file_path: str,
        ours_content: str | None = None,
        theirs_content: str | None = None,
        status: str = "pending",
    ) -> Any: ...


async def _git(
    git_manager: WorktreeGitManager,
    args: list[str],
    cwd: str | Path,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run git via the manager and return (returncode, stdout, stderr)."""
    proc = await git_manager.run_git_command(args, cwd=cwd, timeout=timeout, check=False)
    return proc.returncode, proc.stdout, proc.stderr


async def _git_async(
    git_manager: WorktreeGitManager,
    args: list[str],
    cwd: str | Path,
    timeout: int = 30,
) -> tuple[int, str, str]:
    return await _git(git_manager, args, cwd, timeout)


def _resolve_worktree_path(
    worktree_manager: WorktreeManagerProtocol | None,
    worktree_ref: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (worktree_id, worktree_path, branch_name, error).

    ``worktree_ref`` is a full UUID or a unique id prefix. It is resolved to a
    UUID before any lookup, so a prefix never reaches the uuid column. On error
    the other three are None.
    """
    if not worktree_manager:
        return None, None, None, "worktree_manager not configured"
    try:
        worktree_id = worktree_manager.resolve_reference(worktree_ref)
    except ValueError as exc:
        return None, None, None, str(exc)
    wt = worktree_manager.get(worktree_id)
    if not wt:
        return None, None, None, f"Worktree '{worktree_ref}' not found"
    if not wt.worktree_path:
        return None, None, None, f"Worktree '{worktree_ref}' has no path on disk"
    return worktree_id, wt.worktree_path, wt.branch_name, None


def register_merge_landscape_tools(
    registry: InternalToolRegistry,
    *,
    worktree_manager: WorktreeManagerProtocol | None,
    git_manager: WorktreeGitManager | None,
    merge_storage: MergeStorageProtocol | None = None,
) -> None:
    """Add merge-landscape analytics tools to an existing registry.

    Tools added: analyze_merge_landscape, predict_conflicts,
    cherry_pick_into_worktree, merge_subset, verify_in_worktree,
    inspect_merge_state.
    """

    @registry.tool(
        name="analyze_merge_landscape",
        read_only=True,
        description=(
            "List unmerged worktrees with branch, base, divergence stats, files "
            "touched, last commit time, and the originating task ref. Used to survey a merge campaign before planning."
        ),
    )
    async def analyze_merge_landscape(project_id: str | None = None) -> dict[str, Any]:
        if not worktree_manager:
            return {"success": False, "error": "worktree_manager not configured"}
        if not git_manager:
            return {"success": False, "error": "git_manager not configured"}

        worktrees = worktree_manager.list_worktrees(
            project_id=project_id,
            status=("active", "merged"),
            limit=200,
        )

        out: list[dict[str, Any]] = []
        for wt in worktrees:
            entry: dict[str, Any] = {
                "worktree_id": wt.id,
                "branch": wt.branch_name,
                "base": wt.base_branch,
                "task_ref": wt.task_id,
                "status": wt.status,
                "merge_state": wt.merge_state,
                "created_at": wt.created_at,
            }
            wt_path = wt.worktree_path
            if not wt_path or not Path(wt_path).exists():
                entry["error"] = "worktree_path missing on disk"
                out.append(entry)
                continue

            base_ref = wt.base_branch or "main"
            rc, stdout, stderr = await _git_async(
                git_manager,
                ["rev-list", "--count", f"{base_ref}..HEAD"],
                cwd=wt_path,
            )
            if rc == 0 and stdout.strip().isdigit():
                entry["commits_ahead"] = int(stdout.strip())
            else:
                logger.warning(
                    "rev-list ahead count failed for worktree %s (base=%s): rc=%d stderr=%s; "
                    "the base branch may not be fetched in this worktree.",
                    wt_path,
                    base_ref,
                    rc,
                    stderr.strip(),
                )
                entry["commits_ahead"] = None

            rc, stdout, stderr = await _git_async(
                git_manager,
                ["rev-list", "--count", f"HEAD..{base_ref}"],
                cwd=wt_path,
            )
            if rc == 0 and stdout.strip().isdigit():
                entry["commits_behind"] = int(stdout.strip())
            else:
                logger.warning(
                    "rev-list behind count failed for worktree %s (base=%s): rc=%d stderr=%s; "
                    "the base branch may not be fetched in this worktree.",
                    wt_path,
                    base_ref,
                    rc,
                    stderr.strip(),
                )
                entry["commits_behind"] = None
            ahead = entry["commits_ahead"]
            behind = entry["commits_behind"]
            if wt.status == "merged" and ahead == 0:
                continue
            if wt.status == "merged" and isinstance(ahead, int) and ahead > 0:
                entry["reactivation_required"] = True
            entry["divergence_commits"] = (
                ahead + behind if isinstance(ahead, int) and isinstance(behind, int) else None
            )

            rc, stdout, _ = await _git_async(
                git_manager,
                ["diff", "--name-only", f"{base_ref}...HEAD"],
                cwd=wt_path,
            )
            entry["files_touched"] = (
                [line for line in stdout.splitlines() if line.strip()] if rc == 0 else []
            )

            rc, stdout, _ = await _git_async(
                git_manager,
                ["log", "-1", "--format=%cI", "HEAD"],
                cwd=wt_path,
            )
            entry["last_commit_at"] = stdout.strip() if rc == 0 and stdout.strip() else None

            out.append(entry)

        return {"success": True, "worktrees": out}

    @registry.tool(
        name="predict_conflicts",
        read_only=True,
        description=(
            "Run `git merge-tree` simulations between worktree branches to predict "
            "which pairs will conflict. Returns conflict file lists per pair, plus "
            "predicted conflicts when each branch is merged into the target. If "
            "target_branch is omitted, each worktree uses its stored base_branch. "
            "No side effects."
        ),
    )
    async def predict_conflicts(
        worktree_ids: list[str],
        target_branch: str | None = None,
    ) -> dict[str, Any]:
        if not worktree_manager:
            return {"success": False, "error": "worktree_manager not configured"}
        if not git_manager:
            return {"success": False, "error": "git_manager not configured"}
        if not worktree_ids:
            return {"success": False, "error": "worktree_ids must be non-empty"}

        repo_path = git_manager.repo_path

        branches: list[tuple[str, str, str]] = []
        errors: list[dict[str, str]] = []
        for wid in worktree_ids:
            try:
                resolved_id = worktree_manager.resolve_reference(wid)
            except ValueError as exc:
                errors.append({"worktree_id": wid, "error": str(exc)})
                continue
            worktree = worktree_manager.get(resolved_id)
            if not worktree:
                errors.append({"worktree_id": wid, "error": f"Worktree '{wid}' not found"})
                continue
            branch = getattr(worktree, "branch_name", None)
            if not branch:
                errors.append({"worktree_id": wid, "error": "no branch"})
                continue
            effective_target = target_branch or getattr(worktree, "base_branch", None) or "main"
            branches.append((resolved_id, branch, effective_target))

        async def merge_tree(a: str, b: str) -> tuple[bool, list[str]]:
            rc, stdout, stderr = await _git_async(
                git_manager,
                ["merge-tree", "--write-tree", "--name-only", "--no-messages", a, b],
                cwd=repo_path,
            )
            if rc == 0:
                return True, []
            if rc != 1:
                detail = stderr.strip() or stdout.strip() or "no output"
                raise RuntimeError(f"git merge-tree failed with rc={rc}: {detail}")
            lines = stdout.splitlines()
            conflict_files: list[str] = []
            for line in lines[1:]:
                if not line.strip():
                    break
                conflict_files.append(line.strip())
            return False, conflict_files

        pairs: list[dict[str, Any]] = []
        for i, (a_id, a_branch, _) in enumerate(branches):
            for b_id, b_branch, _ in branches[i + 1 :]:
                try:
                    clean, files = await merge_tree(a_branch, b_branch)
                except RuntimeError as exc:
                    return {"success": False, "error": "merge_tree_failed", "message": str(exc)}
                pairs.append(
                    {
                        "a": a_id,
                        "b": b_id,
                        "clean": clean,
                        "conflict_files": files,
                        "conflict_files_count": len(files),
                    }
                )

        target_predictions: list[dict[str, Any]] = []
        for wid, branch, effective_target in branches:
            try:
                clean, files = await merge_tree(effective_target, branch)
            except RuntimeError as exc:
                return {"success": False, "error": "merge_tree_failed", "message": str(exc)}
            target_predictions.append(
                {
                    "worktree_id": wid,
                    "branch": branch,
                    "target_branch": effective_target,
                    "clean": clean,
                    "conflict_files": files,
                    "conflict_files_count": len(files),
                }
            )

        return {
            "success": True,
            "pairs": pairs,
            "target_predictions": target_predictions,
            "errors": errors,
        }

    @registry.tool(
        name="cherry_pick_into_worktree",
        description=(
            "Cherry-pick one or more commits into a worktree. On conflict, returns "
            "the conflicted file list and leaves CHERRY_PICK_HEAD in place so the "
            "caller can route to the resolve flow or call inspect_merge_state."
        ),
    )
    async def cherry_pick_into_worktree(
        worktree_id: str,
        commits: list[str],
    ) -> dict[str, Any]:
        if not git_manager:
            return {"success": False, "error": "git_manager not configured"}
        if not commits:
            return {"success": False, "error": "commits must be non-empty"}

        _, wt_path, _, err = _resolve_worktree_path(worktree_manager, worktree_id)
        if err or not wt_path:
            return {"success": False, "error": err}

        rc, stdout, stderr = await _git_async(
            git_manager,
            ["cherry-pick", *commits],
            cwd=wt_path,
            timeout=60,
        )
        if rc == 0:
            return {"success": True, "applied": commits, "stdout": stdout.strip()}

        rc2, conflict_stdout, _ = await _git_async(
            git_manager,
            ["diff", "--name-only", "--diff-filter=U"],
            cwd=wt_path,
        )
        conflicts = (
            [line.strip() for line in conflict_stdout.splitlines() if line.strip()]
            if rc2 == 0
            else []
        )
        return {
            "success": False,
            "error": stderr.strip() or "cherry-pick failed",
            "conflicts": conflicts,
        }

    @registry.tool(
        name="merge_subset",
        description=(
            "Pull a subset of paths from another branch into the worktree using "
            "`git checkout source -- <paths>`, stage them, and commit. Use when a "
            "full merge would pull in unwanted changes."
        ),
    )
    async def merge_subset(
        worktree_id: str,
        source_branch: str,
        paths: list[str],
        commit_message: str | None = None,
    ) -> dict[str, Any]:
        if not git_manager:
            return {"success": False, "error": "git_manager not configured"}
        if not source_branch:
            return {"success": False, "error": "source_branch is required"}
        if not paths:
            return {"success": False, "error": "paths must be non-empty"}

        _, wt_path, _, err = _resolve_worktree_path(worktree_manager, worktree_id)
        if err or not wt_path:
            return {"success": False, "error": err}

        rc, _, stderr = await _git_async(
            git_manager,
            ["checkout", source_branch, "--", *paths],
            cwd=wt_path,
        )
        if rc != 0:
            return {
                "success": False,
                "error": f"git checkout failed: {stderr.strip()}",
            }

        rc, _, stderr = await _git_async(
            git_manager,
            ["add", "--", *paths],
            cwd=wt_path,
        )
        if rc != 0:
            return {"success": False, "error": f"git add failed: {stderr.strip()}"}

        message = commit_message or f"merge subset from {source_branch}: {', '.join(paths)}"
        rc, _, stderr = await _git_async(
            git_manager,
            ["commit", "-m", message],
            cwd=wt_path,
            timeout=30,
        )
        if rc != 0:
            return {
                "success": False,
                "error": f"git commit failed: {stderr.strip()}",
            }

        rc, stdout, _ = await _git_async(
            git_manager,
            ["rev-parse", "HEAD"],
            cwd=wt_path,
        )
        commit_sha = stdout.strip() if rc == 0 else None
        return {
            "success": True,
            "paths": paths,
            "source_branch": source_branch,
            "commit_sha": commit_sha,
        }

    @registry.tool(
        name="verify_in_worktree",
        description=(
            "Run an allowlisted verification command (test, build, typecheck, etc.) "
            "inside the worktree directory and return exit code, stdout, stderr. "
            "Used as a post-merge verification gate."
        ),
    )
    async def verify_in_worktree(
        worktree_id: str,
        command: str,
        timeout: int = 300,
        final: bool = False,
    ) -> dict[str, Any]:
        if not command.strip():
            return {
                "success": False,
                "error": "command is required",
                "failure_category": FailureCategory.CODE.value,
            }
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return {
                "success": False,
                "error": f"failed to parse command: {exc}",
                "failure_category": FailureCategory.CODE.value,
            }
        if not argv:
            return {
                "success": False,
                "error": "command is required",
                "failure_category": FailureCategory.CODE.value,
            }
        argv, command_env, rejection = normalize_verification_command(argv)
        if rejection:
            return {
                "success": False,
                "error": rejection,
                "failure_category": FailureCategory.CODE.value,
            }

        _, wt_path, _, err = _resolve_worktree_path(worktree_manager, worktree_id)
        if err or not wt_path:
            return {
                "success": False,
                "error": err,
                "failure_category": FailureCategory.ENVIRONMENT.value,
            }
        rejection = reject_verification_command(argv, wt_path)
        if rejection:
            return {
                "success": False,
                "error": rejection,
                "failure_category": FailureCategory.CODE.value,
            }
        safe_env = verification_environment()
        safe_env.update(command_env)

        try:
            proc = await spawn.create_session_exec(*argv, env=safe_env, cwd=wt_path)
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
                return {
                    "success": False,
                    "error": f"command timed out after {timeout}s",
                    "exit_code": None,
                    "timed_out": True,
                    "failure_category": FailureCategory.TIMEOUT.value,
                }
            exit_code = proc.returncode
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            if exit_code == 0 and final:
                if not git_manager:
                    return {
                        "success": False,
                        "exit_code": exit_code,
                        "stdout": stdout,
                        "stderr": stderr,
                        "error": "git_manager not configured for final clean-tree check",
                        "failure_category": FailureCategory.ENVIRONMENT.value,
                    }
                status_rc, status_stdout, status_stderr = await _git_async(
                    git_manager,
                    ["status", "--porcelain"],
                    cwd=wt_path,
                    timeout=10,
                )
                if status_rc != 0:
                    return {
                        "success": False,
                        "exit_code": exit_code,
                        "stdout": stdout,
                        "stderr": stderr,
                        "error": f"git status failed after verification: {status_stderr.strip()}",
                        "failure_category": FailureCategory.ENVIRONMENT.value,
                    }
                dirty_files = [line for line in status_stdout.splitlines() if line.strip()]
                if dirty_files:
                    return {
                        "success": False,
                        "exit_code": exit_code,
                        "stdout": stdout,
                        "stderr": stderr,
                        "error": "final verification failed: worktree is dirty",
                        "dirty_files": dirty_files,
                        "failure_category": FailureCategory.CODE.value,
                    }
            result = {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
            }
            if exit_code != 0:
                result["failure_category"] = classify_failure(
                    f"{stdout}\n{stderr}",
                    command=command,
                ).value
            return result
        except (OSError, ValueError) as e:
            logger.exception("verify_in_worktree subprocess failed for %s", worktree_id)
            return {
                "success": False,
                "error": f"failed to start command: {e}",
                "failure_category": classify_failure(str(e), command=command).value,
            }

    @registry.tool(
        name="inspect_merge_state",
        read_only=True,
        description=(
            "Detect whether a worktree is mid-merge, mid-cherry-pick, or "
            "mid-rebase, and list any unresolved files. Use to recover orphaned "
            "state before scheduling new merge work."
        ),
    )
    async def inspect_merge_state(worktree_id: str) -> dict[str, Any]:
        if not git_manager:
            return {"success": False, "error": "git_manager not configured"}

        resolved_id, wt_path, _, err = _resolve_worktree_path(worktree_manager, worktree_id)
        if err or not wt_path or not resolved_id:
            return {"success": False, "error": err}

        rc, git_dir_out, stderr = await _git_async(
            git_manager,
            ["rev-parse", "--git-dir"],
            cwd=wt_path,
        )
        if rc != 0:
            return {
                "success": False,
                "error": f"could not resolve git dir: {stderr.strip()}",
            }
        git_dir = (Path(wt_path) / git_dir_out.strip()).resolve()

        has_merge_head = (git_dir / "MERGE_HEAD").exists()
        has_cherry_pick_head = (git_dir / "CHERRY_PICK_HEAD").exists()
        has_rebase = (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

        rc, stdout, _ = await _git_async(
            git_manager,
            ["diff", "--name-only", "--diff-filter=U"],
            cwd=wt_path,
        )
        conflicted_files = (
            [line.strip() for line in stdout.splitlines() if line.strip()] if rc == 0 else []
        )

        if has_merge_head:
            state = "merging"
        elif has_cherry_pick_head:
            state = "cherry-picking"
        elif has_rebase:
            state = "rebasing"
        else:
            state = "clean"

        can_resume = bool(conflicted_files) or state != "clean"

        result: dict[str, Any] = {
            "success": True,
            "state": state,
            "has_merge_head": has_merge_head,
            "has_cherry_pick_head": has_cherry_pick_head,
            "has_rebase_in_progress": has_rebase,
            "conflicted_files": conflicted_files,
            "can_resume": can_resume,
        }
        result.update(
            _active_merge_resolution_payload(
                merge_storage,
                resolved_id,
                conflicted_files=conflicted_files,
            )
        )
        return result


def _active_merge_resolution_payload(
    merge_storage: MergeStorageProtocol | None,
    worktree_id: str,
    *,
    conflicted_files: list[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "active_resolution_id": None,
        "source_branch": None,
        "target_branch": None,
        "conflicts": [],
    }
    if merge_storage is None:
        return payload

    resolution = merge_storage.get_active_resolution(worktree_id)
    if resolution is None and conflicted_files:
        resolution = merge_storage.get_latest_resolution(worktree_id)
    if resolution is None:
        return payload

    unmerged = set(conflicted_files)
    conflicts = merge_storage.list_conflicts(resolution_id=resolution.id)
    missing_paths = sorted(unmerged - {conflict.file_path for conflict in conflicts})
    for file_path in missing_paths:
        try:
            merge_storage.create_conflict(
                resolution_id=resolution.id,
                file_path=file_path,
                status="pending",
            )
        except psycopg.IntegrityError as exc:
            logger.debug(
                "Failed to hydrate merge conflict row for %s in %s: %s",
                file_path,
                resolution.id,
                exc,
            )
        except psycopg.Error:
            logger.warning(
                "Failed to hydrate merge conflict row for %s in %s",
                file_path,
                resolution.id,
                exc_info=True,
            )
            raise
    if missing_paths:
        conflicts = merge_storage.list_conflicts(resolution_id=resolution.id)
    payload.update(
        {
            "active_resolution_id": resolution.id,
            "source_branch": resolution.source_branch,
            "target_branch": resolution.target_branch,
            "conflicts": [
                {
                    "conflict_id": conflict.id,
                    "file_path": conflict.file_path,
                    "status": _inspect_conflict_status(conflict, unmerged),
                    "has_resolved_content": conflict.resolved_content is not None,
                }
                for conflict in conflicts
            ],
        }
    )
    return payload


def _inspect_conflict_status(conflict: Any, unmerged: set[str]) -> str:
    if conflict.file_path in unmerged and conflict.resolved_content is None:
        return "pending"
    return str(conflict.status)
