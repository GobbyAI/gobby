"""Memory dream MCP tool registration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.memory.dream.options import DreamRunOptions

if TYPE_CHECKING:
    from gobby.memory.dream.coordinator import MemoryDreamCoordinator

logger = logging.getLogger(__name__)

_COORDINATOR_UNAVAILABLE = "memory dream coordinator is unavailable"


def register_memory_dream_tools(
    registry: InternalToolRegistry,
    *,
    coordinator_resolver: Callable[[], MemoryDreamCoordinator | None],
    get_project_id: Callable[[], str | None],
) -> None:
    """Register memory dream tools on the gobby-memory registry.

    Triggers are always asynchronous: the daemon-owned coordinator performs
    admission, launches the run when newly admitted, and returns the run ID
    immediately. Progress is observed via ``memory_dream_status``.
    """

    @registry.tool(
        name="memory_dream",
        description=(
            "Start an asynchronous memory dream run and return its run ID; "
            "poll memory_dream_status for progress."
        ),
    )
    async def memory_dream(
        dry_run: bool = False,
        skip_consolidation: bool = False,
        memory_type: str | None = None,
        full_sweep: bool = False,
    ) -> dict[str, Any]:
        coordinator = coordinator_resolver()
        if coordinator is None:
            return {"success": False, "error": _COORDINATOR_UNAVAILABLE}
        project_id = get_project_id()
        if project_id is None:
            # No project context → sweep every scope with due memories, each
            # judged against its own truth digest.
            return await coordinator.trigger_all_due_projects(
                dry_run=dry_run,
                skip_consolidation=skip_consolidation,
                memory_type=memory_type,
                full_sweep=full_sweep,
            )
        return await coordinator.trigger(
            DreamRunOptions(
                dry_run=dry_run,
                skip_consolidation=skip_consolidation,
                memory_type=memory_type,
                project_id=project_id,
                full_sweep=full_sweep,
            )
        )

    @registry.tool(
        name="memory_dream_status",
        description="Return status, durable checkpoint, and summary for a memory dream run.",
    )
    async def memory_dream_status(run_id: str) -> dict[str, Any]:
        coordinator = coordinator_resolver()
        if coordinator is None:
            return {"success": False, "error": _COORDINATOR_UNAVAILABLE}
        return await coordinator.service.status(run_id)

    @registry.tool(
        name="memory_dream_revert",
        description="Revert a completed memory dream run from snapshots.",
    )
    async def memory_dream_revert(run_id: str) -> dict[str, Any]:
        coordinator = coordinator_resolver()
        if coordinator is None:
            return {"success": False, "error": _COORDINATOR_UNAVAILABLE}
        return await coordinator.service.revert(run_id)
