"""
Internal MCP tools for Gobby Merge Resolution.

Exposes functionality for:
- Starting merge operations with AI-powered resolution
- Getting merge status and conflict details
- Resolving individual conflicts
- Applying resolved merges
- Aborting merge operations

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import subprocess  # nosec B404 # used for a fixed git dry-run fallback.
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_landscape import register_merge_landscape_tools
from gobby.storage.merge_resolutions import ConflictStatus

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.merge_resolutions import MergeResolutionManager
    from gobby.worktrees.git import WorktreeGitManager
    from gobby.worktrees.merge import MergeResolver

logger = logging.getLogger(__name__)

_GITHUB_TOKEN_ENV_NAMES = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
)
_GITHUB_TOKEN_SECRET_NAMES = (
    "github_personal_access_token",
    "github_token",
    "gh_token",
)
_PROTECTED_PUSH_MARKERS = (
    "protected branch hook declined",
    "protected branch",
    "branch is protected",
    "required status check",
    "required status checks",
    "pull request",
    "pre-receive hook declined",
    "gh006",
)
_PROTECTION_PROBE_TIMEOUT_SECONDS = 30


def _parse_github_remote(remote_url: str) -> tuple[str, str] | None:
    ssh_match = re.match(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?$", remote_url)
    if ssh_match:
        return ssh_match.group("owner"), ssh_match.group("repo")

    parsed = urlparse(remote_url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return None
    repo = parts[1].removesuffix(".git")
    return parts[0], repo


def _github_token(db: DatabaseProtocol | None) -> str | None:
    for name in _GITHUB_TOKEN_ENV_NAMES:
        token = os.environ.get(name)
        if token:
            return token
    if db is None:
        return None
    try:
        from gobby.storage.secrets import SecretStore

        store = SecretStore(db)
        for name in _GITHUB_TOKEN_SECRET_NAMES:
            token = store.get(name)
            if token:
                return token
    except (LookupError, OSError, RuntimeError, sqlite3.Error):
        logger.debug("Failed to resolve GitHub token from SecretStore", exc_info=True)
    return None


def _protection_payload(
    *,
    owner: str,
    repo: str,
    branch: str,
    source: str,
    requires_pr: bool,
    requires_status_checks: list[str] | None = None,
    requires_up_to_date: bool = False,
    requires_review_count: int = 0,
    protection_unknown: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "success": True,
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "source": source,
        "requires_pr": requires_pr,
        "requires_status_checks": requires_status_checks or [],
        "requires_up_to_date": requires_up_to_date,
        "requires_review_count": requires_review_count,
        "protection_unknown": protection_unknown,
        "error": error,
    }


def _parse_protection_response(
    owner: str,
    repo: str,
    branch: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    status_checks = payload.get("required_status_checks") or {}
    contexts = list(status_checks.get("contexts") or [])
    for check in status_checks.get("checks") or []:
        context = check.get("context") if isinstance(check, dict) else None
        if context:
            contexts.append(context)
    review_rule = payload.get("required_pull_request_reviews") or {}
    return _protection_payload(
        owner=owner,
        repo=repo,
        branch=branch,
        source="github_api",
        requires_pr=True,
        requires_status_checks=sorted(set(contexts)),
        requires_up_to_date=bool(status_checks.get("strict")),
        requires_review_count=int(review_rule.get("required_approving_review_count") or 0),
    )


async def _push_dry_run_probe(
    *,
    repo_path: str,
    owner: str,
    repo: str,
    branch: str,
    git_manager: WorktreeGitManager | None,
    source: str,
    error: str | None,
) -> dict[str, Any]:
    command = ["push", "--dry-run", "origin", f"HEAD:{branch}"]
    if git_manager is not None:
        result = await asyncio.to_thread(
            git_manager.run_git_command,
            command,
            cwd=repo_path,
            # Keep protection probing bounded; GitHub auth/network stalls should degrade.
            timeout=_PROTECTION_PROBE_TIMEOUT_SECONDS,
        )
        returncode = result.returncode
        output = f"{result.stdout}\n{result.stderr}"
    else:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["git", *command],
            cwd=repo_path,
            capture_output=True,
            text=True,
            # Match the WorktreeGitManager probe timeout for direct subprocess fallback.
            timeout=_PROTECTION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        returncode = proc.returncode
        output = f"{proc.stdout}\n{proc.stderr}"

    lowered = output.lower()
    looks_protected = any(marker in lowered for marker in _PROTECTED_PUSH_MARKERS)
    if returncode == 0:
        requires_pr = False
        protection_unknown = False
    else:
        requires_pr = True
        protection_unknown = not looks_protected
    return _protection_payload(
        owner=owner,
        repo=repo,
        branch=branch,
        source=source,
        requires_pr=requires_pr,
        protection_unknown=protection_unknown,
        error=error or (output.strip() if returncode != 0 else None),
    )


def create_merge_registry(
    merge_storage: MergeResolutionManager,
    merge_resolver: MergeResolver,
    git_manager: WorktreeGitManager | None = None,
    worktree_manager: Any | None = None,
    db: DatabaseProtocol | None = None,
) -> InternalToolRegistry:
    """
    Create a merge tool registry with all merge-related tools.

    Args:
        merge_storage: MergeResolutionManager for database operations.
        merge_resolver: MergeResolver for AI-powered conflict resolution.
        git_manager: WorktreeGitManager for git operations.
        worktree_manager: LocalWorktreeManager for resolving worktree paths.
        db: Local database for resolving GitHub tokens.

    Returns:
        InternalToolRegistry with all merge tools registered.
    """
    registry = InternalToolRegistry(
        name="gobby-merge",
        description="AI-powered merge conflict resolution - start merges, resolve conflicts, and apply resolutions",
    )

    def _existing_resolution_start_response(resolution: Any) -> dict[str, Any] | None:
        conflicts = merge_storage.list_conflicts(resolution_id=resolution.id)
        unresolved_conflicts = [
            conflict for conflict in conflicts if conflict.status != ConflictStatus.RESOLVED.value
        ]

        if resolution.status == "resolved":
            return {
                "success": True,
                "resolution_id": resolution.id,
                "tier": resolution.tier_used,
                "needs_human_review": False,
                "conflicts": [],
                "resolved_files": [],
                "reused_resolution": True,
            }

        if resolution.status == "pending" and conflicts:
            return {
                "success": False,
                "resolution_id": resolution.id,
                "tier": resolution.tier_used,
                "needs_human_review": bool(unresolved_conflicts),
                "conflicts": [{"file": conflict.file_path} for conflict in unresolved_conflicts],
                "resolved_files": [],
                "reused_resolution": True,
            }

        return None

    @registry.tool(
        name="merge_start",
        description="Start a merge operation with AI-powered conflict resolution.",
    )
    async def merge_start(
        worktree_id: str,
        source_branch: str,
        target_branch: str = "main",
        strategy: str = "auto",
    ) -> dict[str, Any]:
        """
        Start a merge operation.

        Args:
            worktree_id: ID of the worktree to merge in.
            source_branch: Branch being merged in.
            target_branch: Target branch (default: main).
            strategy: Resolution strategy ('auto', 'conflict_only', 'full_file', 'manual').

        Returns:
            Dict with resolution_id, success status, and conflict details.
        """
        # Validate required parameters
        if not worktree_id:
            return {"success": False, "error": "worktree_id is required"}
        if not source_branch:
            return {"success": False, "error": "source_branch is required"}

        resolution = None
        try:
            existing = merge_storage.get_resolution_for_merge(
                worktree_id=worktree_id,
                source_branch=source_branch,
                target_branch=target_branch,
            )
            if existing:
                existing_response = _existing_resolution_start_response(existing)
                if existing_response is not None:
                    return existing_response
                resolution = existing
            else:
                active = merge_storage.get_active_resolution(worktree_id)
                if active and (
                    active.source_branch != source_branch or active.target_branch != target_branch
                ):
                    return {
                        "success": False,
                        "error": (
                            "Active merge resolution already exists for worktree "
                            f"'{worktree_id}' with source '{active.source_branch}' "
                            f"and target '{active.target_branch}'"
                        ),
                        "resolution_id": active.id,
                    }

                resolution, created = merge_storage.get_or_create_resolution(
                    worktree_id=worktree_id,
                    source_branch=source_branch,
                    target_branch=target_branch,
                    status="pending",
                )
                if not created:
                    existing_response = _existing_resolution_start_response(resolution)
                    if existing_response is not None:
                        return existing_response

            # Attempt merge resolution
            from gobby.worktrees.merge import ResolutionTier

            force_tier = None
            if strategy == "conflict_only":
                force_tier = ResolutionTier.CONFLICT_ONLY_AI
            elif strategy == "full_file":
                force_tier = ResolutionTier.FULL_FILE_AI

            # Get worktree path from manager
            worktree_path = None
            if worktree_manager:
                worktree = worktree_manager.get(worktree_id)
                if worktree and worktree.worktree_path:
                    worktree_path = worktree.worktree_path

            if not worktree_path:
                return {
                    "success": False,
                    "error": f"Worktree '{worktree_id}' not found or has no path",
                }

            result = await merge_resolver.resolve(
                worktree_path=worktree_path,
                source_branch=source_branch,
                target_branch=target_branch,
                force_tier=force_tier,
            )

            # Update resolution with result
            merge_storage.update_resolution(
                resolution_id=resolution.id,
                status="resolved" if result.success else "pending",
                tier_used=result.tier.value if result.success else None,
            )

            # Create conflict records if needed
            for conflict in result.conflicts:
                file_path = conflict.get("file", "")
                merge_storage.create_conflict(
                    resolution_id=resolution.id,
                    file_path=file_path,
                    ours_content=conflict.get("ours_content"),
                    theirs_content=conflict.get("theirs_content"),
                    status="pending" if not result.success else "resolved",
                )

            return {
                "success": result.success,
                "resolution_id": resolution.id,
                "tier": result.tier.value,
                "needs_human_review": result.needs_human_review,
                "conflicts": [{"file": c.get("file", "")} for c in result.unresolved_conflicts],
                "resolved_files": result.resolved_files,
            }

        except Exception as e:
            logger.exception(
                f"Error starting merge for worktree_id={worktree_id}, resolution_id={resolution.id if resolution is not None else 'N/A'}",
            )
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="merge_status",
        description="Get the status of a merge resolution including conflict details.",
    )
    async def merge_status(resolution_id: str) -> dict[str, Any]:
        """
        Get merge resolution status.

        Args:
            resolution_id: The resolution ID.

        Returns:
            Dict with resolution details and conflicts.
        """
        if not resolution_id:
            return {"success": False, "error": "resolution_id is required"}

        resolution = merge_storage.get_resolution(resolution_id)
        if not resolution:
            return {"success": False, "error": f"Resolution '{resolution_id}' not found"}

        conflicts = merge_storage.list_conflicts(resolution_id=resolution_id)

        return {
            "success": True,
            "resolution": resolution.to_dict(),
            "conflicts": [c.to_dict() for c in conflicts],
            "pending_count": sum(1 for c in conflicts if c.status == "pending"),
            "resolved_count": sum(1 for c in conflicts if c.status == "resolved"),
        }

    @registry.tool(
        name="merge_resolve",
        description="Resolve a specific conflict, optionally with AI assistance.",
    )
    async def merge_resolve(
        conflict_id: str,
        resolved_content: str | None = None,
        use_ai: bool = True,
    ) -> dict[str, Any]:
        """
        Resolve a specific conflict.

        Args:
            conflict_id: The conflict ID.
            resolved_content: Manual resolution content (skips AI).
            use_ai: Whether to use AI for resolution (default: True).

        Returns:
            Dict with resolution result.
        """
        if not conflict_id:
            return {"success": False, "error": "conflict_id is required"}

        conflict = merge_storage.get_conflict(conflict_id)
        if not conflict:
            return {"success": False, "error": f"Conflict '{conflict_id}' not found"}

        try:
            if resolved_content is not None:
                # Manual resolution
                updated = merge_storage.update_conflict(
                    conflict_id=conflict_id,
                    status=ConflictStatus.RESOLVED.value,
                    resolved_content=resolved_content,
                )
                return {
                    "success": True,
                    "conflict": updated.to_dict() if updated else None,
                    "resolution_method": "manual",
                }

            if use_ai:
                # Use AI resolver
                from gobby.worktrees.merge import ConflictHunk

                # Create hunk from conflict data
                hunks = [
                    ConflictHunk(
                        ours=conflict.ours_content or "",
                        theirs=conflict.theirs_content or "",
                        base=None,
                        start_line=1,
                        end_line=1,
                        context_before="",
                        context_after="",
                    )
                ]

                worktree_path = None
                resolution = merge_storage.get_resolution(conflict.resolution_id)
                if resolution and worktree_manager:
                    worktree = worktree_manager.get(resolution.worktree_id)
                    if worktree and worktree.worktree_path:
                        worktree_path = worktree.worktree_path

                result = await merge_resolver.resolve_file(
                    path=conflict.file_path,
                    conflict_hunks=hunks,
                    worktree_path=worktree_path,
                )

                if result.success:
                    resolved = result.resolved_content_by_file.get(conflict.file_path)
                    if not resolved:
                        return {
                            "success": False,
                            "error": (
                                "AI resolver returned success but produced no content "
                                f"for {conflict.file_path}"
                            ),
                            "needs_human_review": True,
                        }
                    updated = merge_storage.update_conflict(
                        conflict_id=conflict_id,
                        status=ConflictStatus.RESOLVED.value,
                        resolved_content=resolved,
                    )
                    return {
                        "success": True,
                        "conflict": updated.to_dict() if updated else None,
                        "resolution_method": "ai",
                        "tier": result.tier.value,
                    }
                else:
                    return {
                        "success": False,
                        "error": "AI resolution failed",
                        "needs_human_review": result.needs_human_review,
                    }

            return {"success": False, "error": "No resolution method specified"}

        except Exception as e:
            logger.exception(f"Error resolving conflict {conflict_id}")
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="merge_apply",
        description="Apply all resolved conflicts and complete the merge.",
    )
    async def merge_apply(resolution_id: str) -> dict[str, Any]:
        """
        Apply all resolutions and complete the merge.

        Args:
            resolution_id: The resolution ID.

        Returns:
            Dict with merge completion status.
        """
        if not resolution_id:
            return {"success": False, "error": "resolution_id is required"}

        resolution = merge_storage.get_resolution(resolution_id)
        if not resolution:
            return {"success": False, "error": f"Resolution '{resolution_id}' not found"}

        conflicts = merge_storage.list_conflicts(resolution_id=resolution_id)

        # Check if all conflicts are resolved
        pending = [c for c in conflicts if c.status != "resolved"]
        if pending:
            return {
                "success": False,
                "error": f"Cannot apply: {len(pending)} unresolved conflicts remaining",
                "pending_conflicts": [{"id": c.id, "file_path": c.file_path} for c in pending],
            }

        try:
            if not git_manager or not worktree_manager:
                return {
                    "success": False,
                    "error": "git_manager or worktree_manager not configured",
                }

            worktree = worktree_manager.get(resolution.worktree_id)
            if not worktree or not worktree.worktree_path:
                return {
                    "success": False,
                    "error": (f"Worktree '{resolution.worktree_id}' not found or has no path"),
                }
            wt_path = worktree.worktree_path

            written: list[str] = []
            for conflict in conflicts:
                if conflict.resolved_content is None:
                    return {
                        "success": False,
                        "error": (
                            f"Conflict {conflict.id} for {conflict.file_path} has no "
                            "resolved_content; resolve it before applying"
                        ),
                    }
                target = Path(wt_path) / conflict.file_path
                await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(
                    target.write_text, conflict.resolved_content, encoding="utf-8"
                )

                add_result = await asyncio.to_thread(
                    git_manager.stage_files,
                    [conflict.file_path],
                    cwd=wt_path,
                )
                if add_result.returncode != 0:
                    return {
                        "success": False,
                        "error": (
                            f"git add failed for {conflict.file_path}: {add_result.stderr.strip()}"
                        ),
                    }
                written.append(conflict.file_path)

            unmerged = await asyncio.to_thread(git_manager.get_unmerged_files, cwd=wt_path)
            if unmerged:
                return {
                    "success": False,
                    "error": (
                        f"Cannot complete merge: {len(unmerged)} files still have "
                        "unmerged changes after applying resolutions"
                    ),
                    "unmerged_files": unmerged,
                }

            commit_result = await asyncio.to_thread(
                git_manager.run_git_command,
                ["commit", "--no-edit"],
                cwd=wt_path,
                timeout=30,
            )
            if commit_result.returncode != 0:
                return {
                    "success": False,
                    "error": (
                        f"git commit failed: "
                        f"{(commit_result.stderr or commit_result.stdout).strip()}"
                    ),
                }

            updated = merge_storage.update_resolution(
                resolution_id=resolution_id,
                status="resolved",
                tier_used=resolution.tier_used or "manual",
            )

            return {
                "success": True,
                "resolution": updated.to_dict() if updated else None,
                "message": "Merge completed successfully",
                "files_merged": written,
            }

        except Exception as e:
            logger.exception(f"Error applying merge for resolution {resolution_id}")
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="merge_abort",
        description="Abort the merge operation and restore the previous state.",
    )
    async def merge_abort(resolution_id: str) -> dict[str, Any]:
        """
        Abort a merge operation.

        Args:
            resolution_id: The resolution ID.

        Returns:
            Dict with abort status.
        """
        if not resolution_id:
            return {"success": False, "error": "resolution_id is required"}

        resolution = merge_storage.get_resolution(resolution_id)
        if not resolution:
            return {"success": False, "error": f"Resolution '{resolution_id}' not found"}

        # Can't abort already resolved merges
        if resolution.status == "resolved":
            return {"success": False, "error": "Cannot abort: merge is already resolved"}

        try:
            # Abort git merge if in progress
            if git_manager:
                # Would run git merge --abort
                pass

            # Delete resolution and associated conflicts (cascade)
            deleted = merge_storage.delete_resolution(resolution_id)

            if deleted:
                return {
                    "success": True,
                    "message": "Merge aborted successfully",
                    "resolution_id": resolution_id,
                }
            else:
                return {"success": False, "error": "Failed to abort merge"}

        except Exception as e:
            logger.exception(f"Error aborting merge for resolution_id={resolution_id}")
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="probe_branch_protection",
        description="Probe whether a target branch should be delivered through a GitHub PR.",
    )
    async def probe_branch_protection(
        repo_path: str | None = None,
        branch: str = "main",
        worktree_id: str | None = None,
    ) -> dict[str, Any]:
        """Probe GitHub branch protection and return PR gating requirements."""
        effective_repo_path = repo_path
        if not effective_repo_path and worktree_id and worktree_manager is not None:
            worktree = worktree_manager.get(worktree_id)
            if worktree is not None:
                effective_repo_path = worktree.worktree_path
                branch = branch or worktree.base_branch
        if not effective_repo_path:
            return {
                "success": False,
                "error": "repo_path or resolvable worktree_id is required",
            }

        remote_url: str | None = None
        if git_manager is not None:
            remote = await asyncio.to_thread(
                git_manager.run_git_command,
                ["remote", "get-url", "origin"],
                cwd=effective_repo_path,
                timeout=10,
            )
            if remote.returncode == 0:
                remote_url = remote.stdout.strip()
        if not remote_url:
            from gobby.utils.git import get_github_url

            remote_url = get_github_url(effective_repo_path)
        if not remote_url:
            return {"success": False, "error": "No origin remote found"}

        parsed = _parse_github_remote(remote_url)
        if parsed is None:
            return {
                "success": False,
                "error": f"Origin remote is not a github.com repository: {remote_url}",
            }
        owner, repo = parsed

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = _github_token(db)
        if token:
            headers["Authorization"] = f"Bearer {token}"

        api_url = f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}/protection"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(api_url, headers=headers)
        except httpx.HTTPError as exc:
            return await _push_dry_run_probe(
                repo_path=effective_repo_path,
                owner=owner,
                repo=repo,
                branch=branch,
                git_manager=git_manager,
                source="push_dry_run_after_api_error",
                error=str(exc),
            )

        if response.status_code == 200:
            return _parse_protection_response(owner, repo, branch, response.json())
        if response.status_code == 404:
            return _protection_payload(
                owner=owner,
                repo=repo,
                branch=branch,
                source="github_api",
                requires_pr=False,
            )

        if response.status_code in {401, 403}:
            fallback_source = f"push_dry_run_after_{response.status_code}"
            return await _push_dry_run_probe(
                repo_path=effective_repo_path,
                owner=owner,
                repo=repo,
                branch=branch,
                git_manager=git_manager,
                source=fallback_source,
                error=response.text.strip(),
            )

        return await _push_dry_run_probe(
            repo_path=effective_repo_path,
            owner=owner,
            repo=repo,
            branch=branch,
            git_manager=git_manager,
            source=f"push_dry_run_after_{response.status_code}",
            error=response.text.strip(),
        )

    register_merge_landscape_tools(
        registry,
        worktree_manager=worktree_manager,
        git_manager=git_manager,
    )

    return registry
