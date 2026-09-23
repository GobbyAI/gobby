"""Branch-protection MCP tool registration for gobby-merge."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_github_protection import push_dry_run_probe
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.worktrees.git import WorktreeGitManager


def register_branch_protection_tool(
    registry: InternalToolRegistry,
    *,
    git_manager: WorktreeGitManager | None = None,
    worktree_manager: Any | None = None,
) -> None:
    @registry.tool(
        name="probe_branch_protection",
        read_only=True,
        description="Probe whether a target branch rejects a direct push via git dry-run.",
    )
    async def probe_branch_protection(
        repo_path: str | None = None,
        branch: str | None = None,
        worktree_id: str | None = None,
    ) -> dict[str, Any]:
        """Probe branch protection with a git push dry-run."""
        effective_branch = branch or "main"
        effective_repo_path = repo_path
        if not effective_repo_path and worktree_id and worktree_manager is not None:
            worktree = worktree_manager.get(worktree_id)
            if worktree is not None:
                effective_repo_path = worktree.worktree_path
                effective_branch = branch or worktree.base_branch
        if not effective_repo_path:
            return {
                "success": False,
                "error": "repo_path or resolvable worktree_id is required",
            }

        remote = await daemon_git.run(
            ["remote", "get-url", "origin"],
            cwd=effective_repo_path,
            timeout=10.0,
        )
        remote_url = remote.stdout.strip() if isinstance(remote, GitOk) else None
        if not remote_url:
            return {"success": False, "error": "No origin remote found"}

        return await push_dry_run_probe(
            repo_path=effective_repo_path,
            branch=effective_branch,
            git_manager=git_manager,
            source="push_dry_run",
            error=None,
        )
