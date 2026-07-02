"""Internal MCP tools for Gobby clone management."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gobby.mcp_proxy.tools._clones_cleanup import create_clone_cleanup_registry
from gobby.mcp_proxy.tools._clones_context import CloneRegistryContext
from gobby.mcp_proxy.tools._clones_creation import create_clone_creation_registry
from gobby.mcp_proxy.tools._clones_operations import create_clone_operations_registry
from gobby.mcp_proxy.tools._clones_read import create_clone_read_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

if TYPE_CHECKING:
    from gobby.clones.git import CloneGitManager
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.tasks import LocalTaskManager


def create_clones_registry(
    clone_storage: LocalCloneManager,
    git_manager: CloneGitManager | None,
    project_id: str | None,
    task_manager: LocalTaskManager | None = None,
) -> InternalToolRegistry:
    """
    Create the gobby-clones MCP server registry.

    Args:
        clone_storage: Clone storage manager for CRUD operations
        git_manager: Git manager for clone operations (None when no git repo detected)
        project_id: Default project ID for new clones (None when no project context)
        task_manager: Task manager for resolving task references (#N -> UUID)

    Returns:
        InternalToolRegistry with clone management tools
    """
    ctx = CloneRegistryContext(
        clone_storage=clone_storage,
        git_manager=git_manager,
        project_id=project_id,
        task_manager=task_manager,
    )
    registry = InternalToolRegistry(
        name="gobby-clones",
        description="Git clone management for isolated development",
    )

    registry.merge_from(create_clone_creation_registry(ctx))
    registry.merge_from(create_clone_read_registry(ctx))
    registry.merge_from(create_clone_operations_registry(ctx))
    registry.merge_from(create_clone_cleanup_registry(ctx))

    return registry


__all__ = ["create_clones_registry"]
