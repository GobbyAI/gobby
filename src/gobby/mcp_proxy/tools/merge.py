"""MCP tools for AI-powered merge conflict resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_branch_protection_tool import register_branch_protection_tool
from gobby.mcp_proxy.tools.merge_direct import (
    _GIT_NO_FF_TIER,
    _NO_FF_STRATEGIES,
    _strategy_requests_no_ff,
)
from gobby.mcp_proxy.tools.merge_landscape import register_merge_landscape_tools
from gobby.mcp_proxy.tools.merge_lifecycle import register_merge_lifecycle_tools
from gobby.mcp_proxy.tools.worktrees._merge_fallback import (
    _non_gobby_status_lines,
    _status_path_is_gobby_only,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.merge_resolutions import MergeResolutionManager
    from gobby.worktrees.git import WorktreeGitManager
    from gobby.worktrees.merge import MergeResolver


def create_merge_registry(
    merge_storage: MergeResolutionManager,
    merge_resolver: MergeResolver,
    git_manager: WorktreeGitManager | None = None,
    worktree_manager: Any | None = None,
    db: HubDatabase | None = None,
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
        description=(
            "AI-powered merge conflict resolution - start merges, resolve conflicts, "
            "and apply resolutions"
        ),
    )

    register_merge_lifecycle_tools(
        registry,
        merge_storage=merge_storage,
        merge_resolver=merge_resolver,
        git_manager=git_manager,
        worktree_manager=worktree_manager,
    )
    register_branch_protection_tool(
        registry,
        git_manager=git_manager,
        worktree_manager=worktree_manager,
        db=db,
        async_client_factory=httpx.AsyncClient,
    )
    register_merge_landscape_tools(
        registry,
        worktree_manager=worktree_manager,
        git_manager=git_manager,
        merge_storage=merge_storage,
    )

    return registry


__all__ = [
    "_GIT_NO_FF_TIER",
    "_NO_FF_STRATEGIES",
    "_non_gobby_status_lines",
    "_status_path_is_gobby_only",
    "_strategy_requests_no_ff",
    "create_merge_registry",
]
