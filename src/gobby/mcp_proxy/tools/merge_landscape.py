"""
Internal MCP tools for surveying the merge landscape across worktrees.

These tools support the `merge-orchestrator` agent: they answer "what's
unmerged, what will conflict, what state is each worktree in," and they
provide the surgical primitives (cherry-pick, subset merge, post-merge
verification) needed to drive a merge campaign.

All tools are registered onto the existing `gobby-merge` registry via
`register_merge_landscape_tools` rather than creating a new server.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

if TYPE_CHECKING:
    from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)


def _git(
    git_manager: WorktreeGitManager,
    args: list[str],
    cwd: str | Path,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run git via the manager and return (returncode, stdout, stderr)."""
    proc = git_manager._run_git(args, cwd=cwd, timeout=timeout, check=False)
    return proc.returncode, proc.stdout, proc.stderr


async def _git_async(
    git_manager: WorktreeGitManager,
    args: list[str],
    cwd: str | Path,
    timeout: int = 30,
) -> tuple[int, str, str]:
    return await asyncio.to_thread(_git, git_manager, args, cwd, timeout)


def _resolve_worktree_path(
    worktree_manager: Any | None,
    worktree_id: str,
) -> tuple[str | None, str | None, str | None]:
    """Return (worktree_path, branch_name, error). On error, the others are None."""
    if not worktree_manager:
        return None, None, "worktree_manager not configured"
    wt = worktree_manager.get(worktree_id)
    if not wt:
        return None, None, f"Worktree '{worktree_id}' not found"
    if not wt.worktree_path:
        return None, None, f"Worktree '{worktree_id}' has no path on disk"
    return wt.worktree_path, wt.branch_name, None


def register_merge_landscape_tools(
    registry: InternalToolRegistry,
    *,
    worktree_manager: Any | None,
    git_manager: WorktreeGitManager | None,
) -> None:
    """Add merge-landscape analytics tools to an existing registry.

    Tools added: analyze_merge_landscape, predict_conflicts,
    cherry_pick_into_worktree, merge_subset, verify_in_worktree,
    inspect_merge_state.
    """

    @registry.tool(
        name="analyze_merge_landscape",
        description=(
            "List unmerged worktrees with branch, base, divergence stats, files "
            "touched, last commit time, and the originating task ref. Used by "
            "merge-orchestrator to survey the campaign before planning."
        ),
    )
    async def analyze_merge_landscape(project_id: str | None = None) -> dict[str, Any]:
        if not worktree_manager:
            return {"success": False, "error": "worktree_manager not configured"}
        if not git_manager:
            return {"success": False, "error": "git_manager not configured"}

        worktrees = worktree_manager.list_worktrees(
            project_id=project_id,
            status="active",
            limit=200,
        )

        out: list[dict[str, Any]] = []
        for wt in worktrees:
            entry: dict[str, Any] = {
                "worktree_id": wt.id,
                "branch": wt.branch_name,
                "base": wt.base_branch,
                "task_ref": wt.task_id,
                "merge_state": wt.merge_state,
                "created_at": wt.created_at,
            }
            wt_path = wt.worktree_path
            if not wt_path or not Path(wt_path).exists():
                entry["error"] = "worktree_path missing on disk"
                out.append(entry)
                continue

            base_ref = wt.base_branch or "main"
            rc, stdout, _ = await _git_async(
                git_manager,
                ["rev-list", "--count", f"{base_ref}...HEAD"],
                cwd=wt_path,
            )
            entry["divergence_commits"] = (
                int(stdout.strip()) if rc == 0 and stdout.strip().isdigit() else None
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
        description=(
            "Run `git merge-tree` simulations between worktree branches to predict "
            "which pairs will conflict. Returns conflict file lists per pair, plus "
            "predicted conflicts when each branch is merged into the target. No "
            "side effects."
        ),
    )
    async def predict_conflicts(
        worktree_ids: list[str],
        target_branch: str = "main",
    ) -> dict[str, Any]:
        if not worktree_manager:
            return {"success": False, "error": "worktree_manager not configured"}
        if not git_manager:
            return {"success": False, "error": "git_manager not configured"}
        if not worktree_ids:
            return {"success": False, "error": "worktree_ids must be non-empty"}

        repo_path = git_manager.repo_path

        branches: list[tuple[str, str]] = []
        errors: list[dict[str, str]] = []
        for wid in worktree_ids:
            _, branch, err = _resolve_worktree_path(worktree_manager, wid)
            if err or not branch:
                errors.append({"worktree_id": wid, "error": err or "no branch"})
                continue
            branches.append((wid, branch))

        async def merge_tree(a: str, b: str) -> tuple[bool, list[str]]:
            rc, stdout, _ = await _git_async(
                git_manager,
                ["merge-tree", "--write-tree", "--name-only", "--no-messages", a, b],
                cwd=repo_path,
            )
            if rc == 0:
                return True, []
            lines = stdout.splitlines()
            conflict_files: list[str] = []
            for line in lines[1:]:
                if not line.strip():
                    break
                conflict_files.append(line.strip())
            return False, conflict_files

        pairs: list[dict[str, Any]] = []
        for i, (a_id, a_branch) in enumerate(branches):
            for b_id, b_branch in branches[i + 1 :]:
                clean, files = await merge_tree(a_branch, b_branch)
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
        for wid, branch in branches:
            clean, files = await merge_tree(target_branch, branch)
            target_predictions.append(
                {
                    "worktree_id": wid,
                    "branch": branch,
                    "target_branch": target_branch,
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

        wt_path, _, err = _resolve_worktree_path(worktree_manager, worktree_id)
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

        wt_path, _, err = _resolve_worktree_path(worktree_manager, worktree_id)
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
            "Run a shell command (test, build, typecheck, etc.) inside the "
            "worktree directory and return exit code, stdout, stderr. Used as a "
            "post-merge gate by merge-orchestrator."
        ),
    )
    async def verify_in_worktree(
        worktree_id: str,
        command: str,
        timeout: int = 300,
    ) -> dict[str, Any]:
        if not command:
            return {"success": False, "error": "command is required"}

        wt_path, _, err = _resolve_worktree_path(worktree_manager, worktree_id)
        if err or not wt_path:
            return {"success": False, "error": err}

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=wt_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return {
                    "success": False,
                    "error": f"command timed out after {timeout}s",
                    "exit_code": None,
                    "timed_out": True,
                }
            exit_code = proc.returncode
            return {
                "success": exit_code == 0,
                "exit_code": exit_code,
                "stdout": stdout_b.decode("utf-8", errors="replace"),
                "stderr": stderr_b.decode("utf-8", errors="replace"),
            }
        except (OSError, ValueError) as e:
            logger.exception(f"verify_in_worktree subprocess failed for {worktree_id}")
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="inspect_merge_state",
        description=(
            "Detect whether a worktree is mid-merge, mid-cherry-pick, or "
            "mid-rebase, and list any unresolved files. Use to recover orphaned "
            "state before scheduling new merge work."
        ),
    )
    async def inspect_merge_state(worktree_id: str) -> dict[str, Any]:
        if not git_manager:
            return {"success": False, "error": "git_manager not configured"}

        wt_path, _, err = _resolve_worktree_path(worktree_manager, worktree_id)
        if err or not wt_path:
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

        can_resume = bool(conflicted_files) and state != "clean"

        return {
            "success": True,
            "state": state,
            "has_merge_head": has_merge_head,
            "has_cherry_pick_head": has_cherry_pick_head,
            "has_rebase_in_progress": has_rebase,
            "conflicted_files": conflicted_files,
            "can_resume": can_resume,
        }
