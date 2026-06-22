"""Branch-protection MCP tool registration for gobby-merge."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_github_protection import (
    github_token,
    parse_github_remote,
    parse_protection_response,
    protection_payload,
    push_dry_run_probe,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.git import get_github_url
from gobby.worktrees.git import WorktreeGitManager


def register_branch_protection_tool(
    registry: InternalToolRegistry,
    *,
    git_manager: WorktreeGitManager | None = None,
    worktree_manager: Any | None = None,
    db: HubDatabase | None = None,
    async_client_factory: Callable[..., Any],
) -> None:
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
            remote_url = get_github_url(effective_repo_path)
        if not remote_url:
            return {"success": False, "error": "No origin remote found"}

        parsed = parse_github_remote(remote_url)
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
        token = github_token(db)
        if token:
            headers["Authorization"] = f"Bearer {token}"

        api_url = f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}/protection"
        try:
            async with async_client_factory(timeout=15.0) as client:
                response = await client.get(api_url, headers=headers)
        except httpx.HTTPError as exc:
            return await push_dry_run_probe(
                repo_path=effective_repo_path,
                owner=owner,
                repo=repo,
                branch=branch,
                git_manager=git_manager,
                source="push_dry_run_after_api_error",
                error=str(exc),
            )

        if response.status_code == 200:
            return parse_protection_response(owner, repo, branch, response.json())
        if response.status_code == 404:
            return protection_payload(
                owner=owner,
                repo=repo,
                branch=branch,
                source="github_api",
                requires_pr=False,
            )

        if response.status_code in {401, 403}:
            fallback_source = f"push_dry_run_after_{response.status_code}"
            return await push_dry_run_probe(
                repo_path=effective_repo_path,
                owner=owner,
                repo=repo,
                branch=branch,
                git_manager=git_manager,
                source=fallback_source,
                error=response.text.strip(),
            )

        return await push_dry_run_probe(
            repo_path=effective_repo_path,
            owner=owner,
            repo=repo,
            branch=branch,
            git_manager=git_manager,
            source=f"push_dry_run_after_{response.status_code}",
            error=response.text.strip(),
        )


__all__ = ["register_branch_protection_tool"]
