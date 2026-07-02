"""Shared context for clone MCP tool registries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.clones.git import CloneGitManager
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.tasks import LocalTaskManager


@dataclass(frozen=True, slots=True)
class CloneRegistryContext:
    """Dependencies shared by clone MCP tool modules."""

    clone_storage: LocalCloneManager
    git_manager: CloneGitManager | None
    project_id: str | None
    task_manager: LocalTaskManager | None = None

    def resolve_task_id(self, ref: str) -> str:
        """Resolve task reference (#N, N, UUID) to UUID when a task manager exists."""
        if self.task_manager is None:
            return ref

        from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

        return resolve_task_id_for_mcp(self.task_manager, ref)


__all__ = ["CloneRegistryContext"]
