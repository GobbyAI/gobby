"""Source control API routes for GitHub tab."""

from __future__ import annotations

import logging
import re
import subprocess  # nosec B404 # subprocess needed for git operations
import threading
import time
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Query

from gobby.servers.routes import source_control_github as _source_control_github
from gobby.servers.routes.projects import HIDDEN_PROJECT_NAMES, _checkout_http_error
from gobby.storage.project_checkouts import (
    CheckoutNotFoundError,
    CheckoutSentinelRejectedError,
    MissingMachineContextError,
    require_root,
)
from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS, LocalProjectManager
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)
from gobby.worktrees.deletion import (
    DeletionSurface,
    WorktreeDeletionRequest,
    delete_worktree_transaction,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

_call_github_mcp = _source_control_github._call_github_mcp
_get_github = _source_control_github._get_github
_parse_github_repo = _source_control_github._parse_github_repo

logger = logging.getLogger(__name__)

MAX_PATCH_BYTES = 100_000

# Simple TTL cache: key -> (timestamp, value)
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()
_GITHUB_TTL = 30.0
_GIT_TTL = 10.0
_MAX_CACHE_SIZE = 256

# Strict regex for git ref names — blocks shell metacharacters and traversal
_GIT_REF_RE = re.compile(r"^[a-zA-Z0-9._/\-]+$")


def _validate_git_ref(ref: str, param_name: str = "ref") -> None:
    """Validate a git ref name against injection attacks."""
    if not ref or ".." in ref or ref.startswith("-") or not _GIT_REF_RE.match(ref):
        raise HTTPException(400, f"Invalid git ref for {param_name}: {ref!r}")


def _get_cached(key: str, ttl: float) -> dict[str, Any] | None:
    """Get a cached value if still valid."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry[0]) < ttl:
            return cast(dict[str, Any], entry[1])
        return None


def _set_cached(key: str, value: Any) -> None:
    """Store a value in cache."""
    with _cache_lock:
        if len(_cache) >= _MAX_CACHE_SIZE:
            # Evict oldest entries
            oldest = sorted(_cache, key=lambda k: _cache[k][0])[: _MAX_CACHE_SIZE // 4]
            for k in oldest:
                del _cache[k]
        _cache[key] = (time.time(), value)


def _delete_cached(key: str) -> None:
    """Delete cache entry if present."""
    with _cache_lock:
        _cache.pop(key, None)


async def _run_git(
    args: list[str], cwd: str, timeout: int = 10
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return result (non-blocking)."""
    import asyncio

    return await asyncio.to_thread(
        subprocess.run,  # nosec B603 B607
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _get_project_manager(server: HTTPServer) -> LocalProjectManager:
    """Get a LocalProjectManager from the server."""
    if server.session_manager is None:
        raise HTTPException(503, "Session manager not available")
    return LocalProjectManager(server.session_manager.db)


def _resolve_project(server: HTTPServer, project_id: str | None) -> tuple[str | None, str | None]:
    """Resolve project_id to (checkout root, github_repo).

    A checkout-free sentinel project resolves to (None, github_repo) so callers
    return an empty payload instead of an error. When project_id is None, falls
    back to the first checkout-owning project with a local checkout. A named
    real project with no checkout is HTTP 409, not an empty diff.
    """
    try:
        pm = _get_project_manager(server)
        if project_id:
            project = pm.get(project_id)
            if not project:
                return None, None
            if project.id in CHECKOUT_FREE_PROJECT_IDS:
                return None, project.github_repo
            machine_id = require_local_machine_id(
                None, resource_kind="project_checkout", resource_id=project.id
            )
            try:
                return require_root(pm.db, project.id, machine_id), project.github_repo
            except CheckoutNotFoundError as exc:
                raise _checkout_http_error(exc) from exc
        for project in pm.list():
            if project.name in HIDDEN_PROJECT_NAMES or project.id in CHECKOUT_FREE_PROJECT_IDS:
                continue
            try:
                machine_id = require_local_machine_id(
                    None, resource_kind="project_checkout", resource_id=project.id
                )
                return require_root(pm.db, project.id, machine_id), project.github_repo
            except (CheckoutNotFoundError, CheckoutSentinelRejectedError):
                continue
    except HTTPException as exc:
        if exc.status_code == 409:
            raise
        logger.debug("Failed to resolve project %s: %s", project_id, exc)
    except (
        MissingMachineContextError,
        MachineOwnershipMismatchError,
        CheckoutSentinelRejectedError,
    ) as exc:
        raise _checkout_http_error(exc) from exc
    except (ValueError, OSError) as exc:
        logger.debug("Failed to resolve project %s: %s", project_id, exc)
    return None, None


def create_source_control_router(server: HTTPServer) -> APIRouter:
    """Create the source control API router."""
    router = APIRouter(prefix="/api/source-control", tags=["source-control"])

    @router.get("/status")
    async def get_status(project_id: str | None = None) -> dict[str, Any]:
        """Get source control status overview."""
        repo_path, github_repo = await server.run_db(_resolve_project, server, project_id)
        gh = _get_github(server, project_id)
        github_available = gh.is_available() if gh else False

        current_branch = None
        branch_count = 0
        if repo_path:
            try:
                r = await _run_git(["branch", "--show-current"], repo_path)
                if r.returncode == 0:
                    current_branch = r.stdout.strip()
                r2 = await _run_git(["branch", "--list"], repo_path)
                if r2.returncode == 0:
                    branch_count = len(
                        [line for line in r2.stdout.strip().split("\n") if line.strip()]
                    )
            except (OSError, ValueError) as e:
                logger.warning("Failed to count branches: %s", e)

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

        return {
            "github_available": github_available,
            "github_repo": github_repo,
            "current_branch": current_branch,
            "branch_count": branch_count,
            "worktree_count": worktree_count,
            "clone_count": clone_count,
            "repo_path": repo_path,
        }

    @router.get("/branches")
    async def list_branches(project_id: str | None = None) -> dict[str, Any]:
        """List git branches with ahead/behind info."""
        repo_path, _ = await server.run_db(_resolve_project, server, project_id)
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

                    ahead = 0
                    behind = 0
                    if "[ahead " in track:
                        try:
                            ahead = int(track.split("[ahead ")[1].split("]")[0].split(",")[0])
                        except (ValueError, IndexError):
                            pass
                    if "behind " in track:
                        try:
                            behind = int(track.split("behind ")[1].split("]")[0])
                        except (ValueError, IndexError):
                            pass

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

        repo_path, _ = await server.run_db(_resolve_project, server, project_id)
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
        """List recent commits on a branch.

        Uses GitHub MCP when available for richer data (author avatar, URL),
        falls back to git log.
        """
        _validate_git_ref(branch_name, "branch_name")
        repo_path, github_repo = await server.run_db(_resolve_project, server, project_id)
        if not repo_path:
            return {"commits": []}

        # Try GitHub MCP for richer commit data
        if github_repo and server.services.mcp_manager:
            try:
                from gobby.integrations.github_helper import GitHubMCPHelper

                helper = GitHubMCPHelper(
                    mcp_manager=server.services.mcp_manager,
                    repo_path=repo_path,
                    github_repo=github_repo,
                )
                commits = await helper.list_commits(branch_name, limit=limit)
                if commits:
                    return {"commits": commits}
            except Exception as e:
                logger.debug("GitHubMCPHelper list_commits failed, falling back: %s", e)

        # Fallback: git log
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
        repo_path, _ = await server.run_db(_resolve_project, server, project_id)
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

    @router.get("/prs")
    async def list_pull_requests(
        state: str = "open",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """List pull requests from GitHub."""
        _, github_repo = await server.run_db(_resolve_project, server, project_id)
        gh = _get_github(server, project_id)
        if not gh or not gh.is_available():
            return {"prs": [], "github_available": False}

        parsed = _parse_github_repo(github_repo)
        if not parsed:
            return {"prs": [], "github_available": True, "error": "No GitHub repo configured"}

        cache_key = f"prs:{github_repo}:{state}"
        cached = _get_cached(cache_key, _GITHUB_TTL)
        if cached:
            return cached

        owner, repo = parsed
        try:
            data = await _call_github_mcp(
                server,
                project_id,
                "list_pull_requests",
                {"owner": owner, "repo": repo, "state": state},
            )
            prs = []
            if isinstance(data, list):
                for pr in data:
                    prs.append(
                        {
                            "number": pr.get("number"),
                            "title": pr.get("title"),
                            "state": pr.get("state"),
                            "author": pr.get("user", {}).get("login", ""),
                            "head_branch": pr.get("head", {}).get("ref", ""),
                            "base_branch": pr.get("base", {}).get("ref", ""),
                            "created_at": pr.get("created_at"),
                            "updated_at": pr.get("updated_at"),
                            "draft": pr.get("draft", False),
                            "checks_status": None,
                            "linked_task_id": None,
                        }
                    )
            result = {"prs": prs, "github_available": True}
            _set_cached(cache_key, result)
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Failed to list PRs: %s", e, exc_info=True)
        return {
            "prs": [],
            "github_available": True,
            "error": "Failed to list pull requests",
        }

    @router.get("/prs/{number}")
    async def get_pull_request(
        number: int,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Get pull request details."""
        _, github_repo = await server.run_db(_resolve_project, server, project_id)
        parsed = _parse_github_repo(github_repo)
        if not parsed:
            raise HTTPException(400, "No GitHub repo configured")

        owner, repo = parsed
        data = await _call_github_mcp(
            server,
            project_id,
            "get_pull_request",
            {"owner": owner, "repo": repo, "pull_number": number},
        )
        return {"pr": data, "github_available": True}

    @router.get("/prs/{number}/checks")
    async def get_pr_checks(
        number: int,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Get CI check runs for a PR."""
        _, github_repo = await server.run_db(_resolve_project, server, project_id)
        parsed = _parse_github_repo(github_repo)
        if not parsed:
            raise HTTPException(400, "No GitHub repo configured")

        owner, repo = parsed
        try:
            # Get PR to find head SHA
            pr = await _call_github_mcp(
                server,
                project_id,
                "get_pull_request",
                {"owner": owner, "repo": repo, "pull_number": number},
            )
            head_sha = pr.get("head", {}).get("sha") if isinstance(pr, dict) else None
            if not head_sha:
                return {"checks": [], "status": "unknown"}

            # Get check runs for commit
            checks = await _call_github_mcp(
                server,
                project_id,
                "list_commits",
                {"owner": owner, "repo": repo, "sha": head_sha},
            )
            return {"checks": checks if isinstance(checks, list) else [], "status": "ok"}
        except Exception as e:
            logger.warning("Failed to get PR checks: %s", e, exc_info=True)
        return {
            "checks": [],
            "status": "error",
            "error": "Failed to get pull request checks",
        }

    @router.get("/issues")
    async def list_issues(
        state: str = "open",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """List GitHub issues."""
        _, github_repo = await server.run_db(_resolve_project, server, project_id)
        gh = _get_github(server, project_id)
        if not gh or not gh.is_available():
            return {"issues": [], "github_available": False}

        parsed = _parse_github_repo(github_repo)
        if not parsed:
            return {"issues": [], "github_available": True, "error": "No GitHub repo configured"}

        cache_key = f"issues:{github_repo}:{state}"
        cached = _get_cached(cache_key, _GITHUB_TTL)
        if cached:
            return cached

        owner, repo = parsed
        try:
            data = await _call_github_mcp(
                server,
                project_id,
                "list_issues",
                {"owner": owner, "repo": repo, "state": state},
            )
            issues = []
            if isinstance(data, list):
                for issue in data:
                    # Skip pull requests (GitHub API returns PRs in issues)
                    if issue.get("pull_request"):
                        continue
                    labels = []
                    for lbl in issue.get("labels", []):
                        if isinstance(lbl, dict):
                            labels.append(
                                {"name": lbl.get("name", ""), "color": lbl.get("color", "")}
                            )
                    issues.append(
                        {
                            "number": issue.get("number"),
                            "title": issue.get("title"),
                            "state": issue.get("state"),
                            "author": issue.get("user", {}).get("login", ""),
                            "labels": labels,
                            "created_at": issue.get("created_at"),
                            "updated_at": issue.get("updated_at"),
                            "comments": issue.get("comments", 0),
                        }
                    )
            result = {"issues": issues, "github_available": True}
            _set_cached(cache_key, result)
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Failed to list issues: %s", e, exc_info=True)
        return {
            "issues": [],
            "github_available": True,
            "error": "Failed to list issues",
        }

    @router.get("/issues/{number}")
    async def get_issue(
        number: int,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Get issue details."""
        _, github_repo = await server.run_db(_resolve_project, server, project_id)
        parsed = _parse_github_repo(github_repo)
        if not parsed:
            raise HTTPException(400, "No GitHub repo configured")

        owner, repo = parsed
        try:
            data = await _call_github_mcp(
                server,
                project_id,
                "get_issue",
                {"owner": owner, "repo": repo, "issue_number": number},
            )
            return {"issue": data, "github_available": True}
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Failed to get issue #%s: %s", number, e, exc_info=True)
            raise HTTPException(502, "Failed to fetch issue") from e

    @router.get("/cicd/runs")
    async def list_cicd_runs(
        project_id: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        """List CI/CD workflow runs."""
        _, github_repo = await server.run_db(_resolve_project, server, project_id)
        gh = _get_github(server, project_id)
        if not gh or not gh.is_available():
            return {"runs": [], "github_available": False}

        parsed = _parse_github_repo(github_repo)
        if not parsed:
            return {"runs": [], "github_available": True, "error": "No GitHub repo configured"}

        cache_key = f"cicd:{github_repo}"
        cached = _get_cached(cache_key, _GITHUB_TTL)
        if cached:
            return cached

        owner, repo = parsed
        try:
            data = await _call_github_mcp(
                server,
                project_id,
                "list_workflow_runs",
                {"owner": owner, "repo": repo, "per_page": min(limit, 100)},
            )
            runs = []
            workflow_runs = (
                data.get("workflow_runs", [])
                if isinstance(data, dict)
                else (data if isinstance(data, list) else [])
            )
            for run in workflow_runs:
                runs.append(
                    {
                        "id": run.get("id"),
                        "name": run.get("name"),
                        "status": run.get("status"),
                        "conclusion": run.get("conclusion"),
                        "branch": run.get("head_branch"),
                        "event": run.get("event"),
                        "created_at": run.get("created_at"),
                        "html_url": run.get("html_url"),
                    }
                )
            result = {"runs": runs, "github_available": True}
            _set_cached(cache_key, result)
            return result
        except Exception as e:
            logger.warning("Failed to list CI/CD runs: %s", e, exc_info=True)
        return {
            "runs": [],
            "github_available": True,
            "error": "Failed to list CI/CD runs",
        }

    # --- Worktrees ---

    @router.get("/worktrees")
    async def list_worktrees(
        project_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List worktrees."""
        if not server.services.worktree_storage:
            return {"worktrees": []}

        wts = await server.run_db(
            server.services.worktree_storage.list_worktrees,
            project_id=project_id,
            status=status,
        )
        return {"worktrees": [wt.to_dict() for wt in wts]}

    @router.get("/worktrees/stats")
    async def get_worktree_stats(
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Get worktree statistics."""
        if not server.services.worktree_storage or not project_id:
            return {"stats": {}}

        stats = await server.run_db(server.services.worktree_storage.count_by_status, project_id)
        return {"stats": stats}

    @router.delete("/worktrees/{worktree_id}")
    async def delete_worktree(worktree_id: str) -> dict[str, Any]:
        """Delete a worktree."""
        worktree_storage = server.services.worktree_storage
        if worktree_storage is None:
            raise HTTPException(503, "Worktree storage not available")

        def resolve_git_manager(worktree: Any) -> Any:
            fallback = server.services.git_manager
            if fallback is None:
                return None
            from gobby.worktrees.git import WorktreeGitManager

            try:
                repo_path, _ = _resolve_project(server, worktree.project_id)
                if repo_path:
                    return WorktreeGitManager(repo_path)
            except (ValueError, OSError):
                pass
            return fallback

        request = WorktreeDeletionRequest(
            worktree_id=worktree_id,
            surface=DeletionSurface.HTTP,
        )
        try:
            result = await server.services.run_worktree_delete(
                lambda boundary: delete_worktree_transaction(
                    boundary,
                    request=request,
                    worktree_storage=worktree_storage,
                    resolve_git_manager=resolve_git_manager,
                    task_manager=server.services.task_manager,
                )
            )
        except MachineOwnershipMismatchError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        if not result.found:
            raise HTTPException(404, "Worktree not found")

        response: dict[str, Any] = {
            "success": result.success,
            "id": worktree_id,
            "git_deleted": result.git_deleted,
        }
        if not result.git_deleted:
            response["git_error"] = result.error
            response["message"] = "Git worktree deletion failed; DB record was preserved"
        return response

    @router.post("/worktrees/cleanup")
    async def cleanup_worktrees(
        project_id: str | None = None,
        hours: int = 24,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Cleanup stale worktrees."""
        if not server.services.worktree_storage or not project_id:
            return {"candidates": [], "cleaned": 0}

        stale = await server.run_db(
            server.services.worktree_storage.cleanup_stale,
            project_id,
            hours=hours,
            dry_run=dry_run,
        )
        return {
            "candidates": [wt.to_dict() for wt in stale],
            "cleaned": 0 if dry_run else len(stale),
            "dry_run": dry_run,
        }

    @router.post("/worktrees/{worktree_id}/sync")
    async def sync_worktree(
        worktree_id: str,
        source_branch: str | None = None,
    ) -> dict[str, Any]:
        """Sync a worktree with its base branch."""
        if not server.services.worktree_storage:
            raise HTTPException(503, "Worktree storage not available")

        try:
            wt = await server.run_db(server.services.worktree_storage.get, worktree_id)
        except MachineOwnershipMismatchError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        if not wt:
            raise HTTPException(404, "Worktree not found")

        if server.services.git_manager:
            result = await server.run_db(
                server.services.git_manager.sync_from_main,
                wt.worktree_path,
                base_branch=wt.base_branch,
                source_branch=source_branch,
            )
            return {
                "success": result.success,
                "message": result.message,
                "id": worktree_id,
                "source_branch": source_branch or wt.base_branch,
            }

        raise HTTPException(503, "Git manager not available")

    # --- Clones ---

    @router.get("/clones")
    async def list_clones(
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """List clones."""
        if not server.services.clone_storage:
            return {"clones": []}

        clones = await server.run_db(
            server.services.clone_storage.list_clones, project_id=project_id
        )
        return {"clones": [c.to_dict() for c in clones]}

    @router.delete("/clones/{clone_id}")
    async def delete_clone(clone_id: str) -> dict[str, Any]:
        """Delete a clone."""
        if not server.services.clone_storage:
            raise HTTPException(503, "Clone storage not available")

        try:
            clone = await server.run_db(server.services.clone_storage.get, clone_id)
        except MachineOwnershipMismatchError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        if not clone:
            raise HTTPException(404, "Clone not found")

        deleted = await server.run_db(server.services.clone_storage.delete, clone_id)
        return {"success": deleted, "id": clone_id}

    @router.post("/clones/{clone_id}/sync")
    async def sync_clone(clone_id: str) -> dict[str, Any]:
        """Sync a clone."""
        if not server.services.clone_storage:
            raise HTTPException(503, "Clone storage not available")

        try:
            clone = await server.run_db(server.services.clone_storage.get, clone_id)
        except MachineOwnershipMismatchError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        if not clone:
            raise HTTPException(404, "Clone not found")

        await server.run_db(server.services.clone_storage.record_sync, clone_id)
        return {"success": True, "id": clone_id}

    return router
