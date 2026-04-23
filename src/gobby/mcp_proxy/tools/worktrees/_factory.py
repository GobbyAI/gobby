"""Factory function for creating the worktree tool registry.

Orchestrates the creation of all worktree tool sub-registries and merges
them into a unified registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees._cleanup import create_cleanup_registry
from gobby.mcp_proxy.tools.worktrees._context import RegistryContext
from gobby.mcp_proxy.tools.worktrees._create import create_create_registry
from gobby.mcp_proxy.tools.worktrees._crud import create_crud_registry
from gobby.mcp_proxy.tools.worktrees._lifecycle import create_lifecycle_registry
from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.worktrees.git import WorktreeGitManager


def create_worktrees_registry(
    worktree_storage: LocalWorktreeManager,
    git_manager: WorktreeGitManager | None = None,
    project_id: str | None = None,
    session_manager: SessionManager | None = None,
    task_manager: LocalTaskManager | None = None,
) -> InternalToolRegistry:
    """Create a worktree tool registry with all worktree-related tools.

    Args:
        worktree_storage: LocalWorktreeManager for database operations.
        git_manager: WorktreeGitManager for git operations.
        project_id: Default project ID for operations.
        session_manager: Session manager for resolving session references.
        task_manager: LocalTaskManager for resolving task references.

    Returns:
        InternalToolRegistry with all worktree tools registered.
    """
    ctx = RegistryContext(
        worktree_storage=worktree_storage,
        git_manager=git_manager,
        project_id=project_id,
        session_manager=session_manager,
        task_manager=task_manager,
    )

    registry = InternalToolRegistry(
        name="gobby-worktrees",
        description="Git worktree management - create, manage, and cleanup isolated development directories",
    )

    # Merge all sub-registries
    for sub_factory in (
        create_create_registry,
        create_crud_registry,
        create_lifecycle_registry,
        create_sync_registry,
        create_cleanup_registry,
    ):
        sub_registry = sub_factory(ctx)
        registry.merge_from(sub_registry)

    return registry
