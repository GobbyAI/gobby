"""Source control API routes for git-local status, branches, worktrees, and clones."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess  # nosec B404 # subprocess needed for git operations
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query

from gobby.servers.routes.source_control_git import (
    _GIT_TTL,
    _delete_cached,
    _run_git,
    parse_upstream_track,
)
from gobby.servers.routes.source_control_git import (
    _MAX_CACHE_SIZE as _MAX_CACHE_SIZE,
)
from gobby.servers.routes.source_control_git import (
    _cache as _cache,
)
from gobby.servers.routes.source_control_git import (
    _get_cached as _get_cached,
)
from gobby.servers.routes.source_control_git import (
    _get_project_manager as _get_project_manager,
)
from gobby.servers.routes.source_control_git import (
    _resolve_project as _resolve_project,
)
from gobby.servers.routes.source_control_git import (
    _set_cached as _set_cached,
)
from gobby.servers.routes.source_control_worktrees import (
    create_source_control_worktrees_router,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

MAX_PATCH_BYTES = 100_000

# Strict regex for git ref names — blocks shell metacharacters and traversal
_GIT_REF_RE = re.compile(r"^[a-zA-Z0-9._/\-]+$")


def _validate_git_ref(ref: str, param_name: str = "ref") -> None:
    """Validate a git ref name against injection attacks."""
    if not ref or ".." in ref or ref.startswith("-") or not _GIT_REF_RE.match(ref):
        raise HTTPException(400, f"Invalid git ref for {param_name}: {ref!r}")


def _require_git_success(result: subprocess.CompletedProcess[str], operation: str) -> None:
    if result.returncode == 0:
        return
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    raise HTTPException(503, f"{operation} unavailable: {detail}")


def create_source_control_router(server: HTTPServer) -> APIRouter:
    """Create the source control API router."""
    router = APIRouter(prefix="/api/source-control", tags=["source-control"])
    router.include_router(
        create_source_control_worktrees_router(server, validate_git_ref=_validate_git_ref)
    )

    @router.get("/status")
    async def get_status(project_id: str | None = None) -> dict[str, Any]:
        """Get source control status overview."""
        repo_path = await server.run_db(_resolve_project, server, project_id)

        cache_key = f"status:{project_id or 'default'}"
        cached = _get_cached(cache_key, _GIT_TTL)
        if cached:
            return cached

        current_branch = None
        branch_count = 0
        ahead = None
        behind = None
        if repo_path:
            try:
                branch_result, list_result = await asyncio.gather(
                    _run_git(["branch", "--show-current"], repo_path),
                    _run_git(["branch", "--list"], repo_path),
                )
                _require_git_success(branch_result, "Current branch")
                _require_git_success(list_result, "Branch list")
                current_branch = branch_result.stdout.strip()
                branch_count = len(
                    [line for line in list_result.stdout.strip().split("\n") if line.strip()]
                )
                if current_branch:
                    tracking = await _run_git(
                        [
                            "for-each-ref",
                            "--format=%(upstream:short)\t%(upstream:track)",
                            f"refs/heads/{current_branch}",
                        ],
                        repo_path,
                    )
                    _require_git_success(tracking, "Upstream status")
                    upstream, _, track = tracking.stdout.rstrip("\n").partition("\t")
                    if upstream:
                        ahead, behind = parse_upstream_track(track)
            except subprocess.TimeoutExpired:
                raise HTTPException(504, "Git status timed out") from None
            except OSError as exc:
                raise HTTPException(503, f"Git status unavailable: {exc}") from exc

        worktree_count = 0
        clone_count = 0
        if server.services.worktree_storage:
            wts = await server.run_db(
                server.services.worktree_storage.list_worktrees, project_id=project_id
            )
            worktree_count = len(wts)
        if server.services.clone_storage:
            cls = await server.run_db(
                server.services.clone_storage.list_clones, project_id=project_id
            )
            clone_count = len(cls)

        result = {
            "current_branch": current_branch,
            "branch_count": branch_count,
            "worktree_count": worktree_count,
            "clone_count": clone_count,
            "repo_path": repo_path,
            "ahead": ahead,
            "behind": behind,
        }
        _set_cached(cache_key, result)
        return result

    @router.get("/branches")
    async def list_branches(project_id: str | None = None) -> dict[str, Any]:
        """List git branches with ahead/behind info."""
        repo_path = await server.run_db(_resolve_project, server, project_id)
        if not repo_path:
            return {"branches": [], "current_branch": None}

        cache_key = f"branches:{project_id or 'default'}"
        cached = _get_cached(cache_key, _GIT_TTL)
        if cached:
            return cached

        branches = []
        current_branch = None

        try:
            r = await _run_git(["branch", "--show-current"], repo_path)
            if r.returncode == 0:
                current_branch = r.stdout.strip()

            # Local branches with details
            r = await _run_git(
                [
                    "for-each-ref",
                    "--format=%(refname:short)\t%(upstream:short)\t%(upstream:track)\t%(committerdate:iso8601)",
                    "refs/heads/",
                ],
                repo_path,
                timeout=15,
            )
            if r.returncode == 0:
                for line in r.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("\t")
                    name = parts[0]
                    track = parts[2] if len(parts) > 2 else ""
                    date = parts[3] if len(parts) > 3 else ""

                    ahead, behind = parse_upstream_track(track)

                    # Check if branch has a worktree
                    worktree_id = None
                    if server.services.worktree_storage and project_id:
                        wt = await server.run_db(
                            server.services.worktree_storage.get_by_branch, project_id, name
                        )
                        if wt:
                            worktree_id = wt.id

                    branches.append(
                        {
                            "name": name,
                            "is_current": name == current_branch,
                            "is_remote": False,
                            "ahead": ahead,
                            "behind": behind,
                            "last_commit_date": date,
                            "worktree_id": worktree_id,
                        }
                    )

            # Remote branches
            r = await _run_git(
                [
                    "for-each-ref",
                    "--format=%(refname:short)\t%(committerdate:iso8601)",
                    "refs/remotes/origin/",
                ],
                repo_path,
                timeout=15,
            )
            if r.returncode == 0:
                local_names = {b["name"] for b in branches}
                for line in r.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("\t")
                    full_name = parts[0]
                    date = parts[1] if len(parts) > 1 else ""
                    # Strip origin/ prefix
                    short = full_name[7:] if full_name.startswith("origin/") else full_name
                    if short == "HEAD" or short in local_names:
                        continue
                    branches.append(
                        {
                            "name": short,
                            "is_current": False,
                            "is_remote": True,
                            "ahead": 0,
                            "behind": 0,
                            "last_commit_date": date,
                            "worktree_id": None,
                        }
                    )

        except (subprocess.SubprocessError, OSError) as e:
            logger.warning("Failed to list branches: %s", e)
            return {"branches": branches, "current_branch": current_branch}

        result = {"branches": branches, "current_branch": current_branch}
        _set_cached(cache_key, result)
        return result

    @router.post("/branches/checkout")
    async def checkout_branch(
        payload: dict[str, str],
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Checkout an existing local branch in the main project repository."""
        branch_name = payload.get("branch_name", "")
        _validate_git_ref(branch_name, "branch_name")

        repo_path = await server.run_db(_resolve_project, server, project_id)
        if not repo_path:
            raise HTTPException(400, "No repository path for project")

        try:
            exists = await _run_git(
                ["show-ref", "--verify", f"refs/heads/{branch_name}"],
                repo_path,
            )
            if exists.returncode != 0:
                raise HTTPException(404, f"Local branch not found: {branch_name}")

            switched = await _run_git(["switch", branch_name], repo_path, timeout=30)
            if switched.returncode != 0:
                detail = (
                    switched.stderr.strip()
                    or switched.stdout.strip()
                    or f"Failed to switch branch: {branch_name}"
                )
                raise HTTPException(409, detail)

            current = await _run_git(["branch", "--show-current"], repo_path)
            current_branch = current.stdout.strip() if current.returncode == 0 else branch_name
        except HTTPException:
            raise
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Branch checkout timed out") from None
        except Exception as e:
            logger.warning("Failed to checkout branch: %s", e, exc_info=True)
            raise HTTPException(500, "Failed to checkout branch") from e

        _delete_cached(f"branches:{project_id or 'default'}")
        _delete_cached(f"status:{project_id or 'default'}")
        return {
            "success": True,
            "current_branch": current_branch,
            "repo_path": repo_path,
        }

    @router.get("/branches/{branch_name:path}/commits")
    async def list_branch_commits(
        branch_name: str,
        project_id: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        """List recent commits on a branch via git log."""
        _validate_git_ref(branch_name, "branch_name")
        repo_path = await server.run_db(_resolve_project, server, project_id)
        if not repo_path:
            return {"commits": []}

        commits = []
        try:
            r = await _run_git(
                [
                    "log",
                    branch_name,
                    f"--max-count={min(limit, 100)}",
                    "--format=%H\t%h\t%s\t%an\t%aI",
                ],
                repo_path,
                timeout=15,
            )
            if r.returncode == 0:
                for line in r.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("\t", 4)
                    if len(parts) >= 5:
                        commits.append(
                            {
                                "sha": parts[0],
                                "short_sha": parts[1],
                                "message": parts[2],
                                "author": parts[3],
                                "date": parts[4],
                            }
                        )
        except Exception as e:
            logger.warning("Failed to list commits for %s: %s", branch_name, e)

        return {"commits": commits}

    @router.get("/diff")
    async def get_diff(
        base: str = "main",
        head: str = "HEAD",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Get diff between two refs."""
        _validate_git_ref(base, "base")
        _validate_git_ref(head, "head")
        repo_path = await server.run_db(_resolve_project, server, project_id)
        if not repo_path:
            raise HTTPException(400, "No repository path for project")

        try:
            # Diff stat
            stat_r = await _run_git(
                ["diff", "--stat", f"{base}...{head}"],
                repo_path,
                timeout=30,
            )
            # File list
            files_r = await _run_git(
                ["diff", "--name-status", f"{base}...{head}"],
                repo_path,
                timeout=30,
            )
            # Full patch
            patch_r = await _run_git(
                ["diff", f"{base}...{head}"],
                repo_path,
                timeout=30,
            )

            files = []
            if files_r.returncode == 0:
                for line in files_r.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        files.append({"status": parts[0], "path": parts[1]})

            return {
                "diff_stat": stat_r.stdout if stat_r.returncode == 0 else "",
                "files": files,
                "patch": patch_r.stdout[:MAX_PATCH_BYTES] if patch_r.returncode == 0 else "",
            }
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Diff computation timed out") from None
        except Exception as e:
            logger.warning("Failed to compute diff: %s", e, exc_info=True)
            raise HTTPException(500, "Failed to compute diff") from e

    return router
