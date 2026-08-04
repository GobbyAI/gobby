"""Registry context for worktree tools.

Provides RegistryContext dataclass that bundles shared state and helpers
used across worktree tool modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from gobby.utils.project_context import get_project_context

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.worktrees.git import WorktreeGitManager


@dataclass
class RegistryContext:
    """Shared context for worktree tool registries.

    Bundles managers and helper methods used across all worktree tools.
    """

    worktree_storage: LocalWorktreeManager
    git_manager: WorktreeGitManager | None = None
    project_id: str | None = None
    session_manager: SessionManager | None = None
    task_manager: LocalTaskManager | None = None

    def resolve_session_id(self, ref: str) -> str:
        """Resolve session reference (#N, N, UUID, or prefix) to UUID."""
        if self.session_manager is None:
            return ref
        ctx = get_project_context()
        proj_id = ctx.get("id") if ctx else self.project_id
        return str(self.session_manager.resolve_session_reference(ref, proj_id))

    def resolve_task_id(self, ref: str) -> str:
        """Resolve task reference (#N, N, UUID) to UUID."""
        if self.task_manager is None:
            return ref
        from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

        return resolve_task_id_for_mcp(self.task_manager, ref)
