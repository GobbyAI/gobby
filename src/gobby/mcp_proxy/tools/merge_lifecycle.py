"""Registration helpers for merge lifecycle MCP tools."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_abort_tool import register_merge_abort_tool
from gobby.mcp_proxy.tools.merge_apply_tool import register_merge_apply_tool
from gobby.mcp_proxy.tools.merge_resolve_tool import register_merge_resolve_tool
from gobby.mcp_proxy.tools.merge_start_tool import register_merge_start_tool
from gobby.mcp_proxy.tools.merge_status_tool import register_merge_status_tool
from gobby.storage.merge_resolutions import MergeResolutionManager
from gobby.worktrees.git import WorktreeGitManager
from gobby.worktrees.merge import MergeResolver


def register_merge_lifecycle_tools(
    registry: InternalToolRegistry,
    *,
    merge_storage: MergeResolutionManager,
    merge_resolver: MergeResolver,
    git_manager: WorktreeGitManager | None = None,
    worktree_manager: Any | None = None,
) -> None:
    register_merge_start_tool(
        registry,
        merge_storage=merge_storage,
        merge_resolver=merge_resolver,
        git_manager=git_manager,
        worktree_manager=worktree_manager,
    )
    register_merge_status_tool(
        registry,
        merge_storage=merge_storage,
        git_manager=git_manager,
        worktree_manager=worktree_manager,
    )
    register_merge_resolve_tool(
        registry,
        merge_storage=merge_storage,
        merge_resolver=merge_resolver,
        worktree_manager=worktree_manager,
    )
    register_merge_apply_tool(
        registry,
        merge_storage=merge_storage,
        git_manager=git_manager,
        worktree_manager=worktree_manager,
    )
    register_merge_abort_tool(
        registry,
        merge_storage=merge_storage,
        git_manager=git_manager,
        worktree_manager=worktree_manager,
    )
