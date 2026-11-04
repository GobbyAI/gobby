"""The MCP tools that mutate Git, as one list rule conditions can test."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Each tool merges into, rewrites, resolves or pushes a branch or its in-progress merge.
GIT_MUTATING_MCP_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("gobby-worktrees", "sync_worktree"),
        ("gobby-worktrees", "merge_worktree"),
        ("gobby-worktrees", "push_branch"),
        ("gobby-clones", "sync_clone"),
        ("gobby-clones", "merge_clone"),
        ("gobby-merge", "merge_start"),
        ("gobby-merge", "merge_resolve"),
        ("gobby-merge", "merge_apply"),
        ("gobby-merge", "merge_abort"),
        ("gobby-merge", "cherry_pick_into_worktree"),
        ("gobby-merge", "merge_subset"),
    }
)


def git_mutating_mcp_call(event_data: Mapping[str, Any] | None) -> bool:
    """Whether the event calls an MCP tool that mutates Git."""
    if not isinstance(event_data, Mapping):
        return False
    return (event_data.get("mcp_server"), event_data.get("mcp_tool")) in GIT_MUTATING_MCP_TOOLS
