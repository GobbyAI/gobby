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
import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

if TYPE_CHECKING:
    from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)

_ALLOWED_VERIFICATION_COMMANDS = {
    "git",
    "npm",
    "pnpm",
    "uv",
    "yarn",
}
_ALLOWED_GIT_VERIFICATION_SUBCOMMANDS = {"diff", "ls-files", "rev-parse", "status"}
_BLOCKED_GIT_DIFF_FLAGS = {"--ext-diff", "--no-index"}
_SAFE_ENV_KEYS = {"HOME", "LANG", "LC_ALL", "PATH", "TERM", "TMPDIR"}


class WorktreeManagerProtocol(Protocol):
    def get(self, worktree_id: str) -> Any | None: ...

    def list_worktrees(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
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


def _git(
    git_manager: WorktreeGitManager,
    args: list[str],
    cwd: str | Path,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run git via the manager and return (returncode, stdout, stderr)."""
    proc = git_manager.run_git_command(args, cwd=cwd, timeout=timeout, check=False)
    return proc.returncode, proc.stdout, proc.stderr


async def _git_async(
    git_manager: WorktreeGitManager,
    args: list[str],
    cwd: str | Path,
    timeout: int = 30,
) -> tuple[int, str, str]:
    return await asyncio.to_thread(_git, git_manager, args, cwd, timeout)


def _resolve_worktree_path(
    worktree_manager: WorktreeManagerProtocol | None,
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


def _verification_environment() -> dict[str, str]:
    """Return a minimal environment for verification subprocesses."""
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS}
    env["CI"] = "1"
    if "NO_COLOR" in os.environ:
        env["NO_COLOR"] = os.environ["NO_COLOR"]
    return env


def _reject_git_verification_args(args: list[str]) -> str | None:
    if not args:
        return "git verification requires a subcommand"

    subcommand = args[0]
    if subcommand not in _ALLOWED_GIT_VERIFICATION_SUBCOMMANDS:
        return f"git subcommand '{subcommand}' is not permitted for verification"

    if subcommand == "diff":
        for arg in args[1:]:
            if arg in _BLOCKED_GIT_DIFF_FLAGS or arg.startswith("--output"):
                return f"git diff flag '{arg}' is not permitted for verification"
    return None


def _reject_verification_command(argv: list[str]) -> str | None:
    executable = Path(argv[0]).name
    if executable not in _ALLOWED_VERIFICATION_COMMANDS:
        return f"verification command '{executable}' is not permitted"

    if executable == "git":
        return _reject_git_verification_args(argv[1:])

    if executable in {"npm", "pnpm", "yarn"}:
        if len(argv) < 3 or argv[1] != "run":
            return f"{executable} verification must use 'run <script>'"
        return None

    if executable == "uv":
        if len(argv) >= 3 and argv[1] == "run" and argv[2] in {"mypy", "pytest", "ruff"}:
            return None
        return "uv verification must use 'run pytest', 'run ruff', or 'run mypy'"

    return f"verification command '{executable}' is not permitted"


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
        for i, (a_id, a_branch) in enumerate(branches):
            for b_id, b_branch in branches[i + 1 :]:
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
        for wid, branch in branches:
            try:
                clean, files = await merge_tree(target_branch, branch)
            except RuntimeError as exc:
                return {"success": False, "error": "merge_tree_failed", "message": str(exc)}
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
            "Run an allowlisted verification command (test, build, typecheck, etc.) "
            "inside the worktree directory and return exit code, stdout, stderr. "
            "Used as a post-merge gate by merge-orchestrator."
        ),
    )
    async def verify_in_worktree(
        worktree_id: str,
        command: str,
        timeout: int = 300,
        final: bool = False,
    ) -> dict[str, Any]:
        _ = final
        if not command.strip():
            return {"success": False, "error": "command is required"}
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return {"success": False, "error": f"failed to parse command: {exc}"}
        if not argv:
            return {"success": False, "error": "command is required"}
        rejection = _reject_verification_command(argv)
        if rejection:
            return {"success": False, "error": rejection}

        wt_path, _, err = _resolve_worktree_path(worktree_manager, worktree_id)
        if err or not wt_path:
            return {"success": False, "error": err}
        safe_env = _verification_environment()

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=wt_path,
                env=safe_env,
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
            logger.exception("verify_in_worktree subprocess failed for %s", worktree_id)
            return {"success": False, "error": f"failed to start command: {e}"}

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
                worktree_id,
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
        except Exception as exc:
            logger.debug(
                "Failed to hydrate merge conflict row for %s in %s: %s",
                file_path,
                resolution.id,
                exc,
            )
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
