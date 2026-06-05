"""Memory dream MCP tool registration."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.memory.dream.service import DreamRunOptions, MemoryDreamService

_BACKGROUND_DREAM_TASKS: set[asyncio.Task[Any]] = set()


def register_memory_dream_tools(
    registry: Any,
    *,
    memory_manager: Any,
    llm_service: Any | None,
    config: Any | None,
    get_project_id: Any,
) -> None:
    """Register memory dream tools on the gobby-memory registry."""

    def _service() -> MemoryDreamService:
        dream_config = getattr(getattr(config, "memory", None), "dream", None)
        return MemoryDreamService(
            memory_manager=memory_manager,
            dream_config=dream_config,
            llm_service=llm_service,
        )

    @registry.tool(
        name="memory_dream",
        description="Review stale memories, validate a dream plan, snapshot mutations, and apply it.",
    )
    async def memory_dream(
        dry_run: bool = False,
        wait: bool = True,
        skip_consolidation: bool = False,
        memory_type: str | None = None,
    ) -> dict[str, Any]:
        service = _service()
        options = DreamRunOptions(
            dry_run=dry_run,
            skip_consolidation=skip_consolidation,
            memory_type=memory_type,
            project_id=get_project_id(),
        )
        if wait:
            return await service.run(options)
        started = service.start(options)
        if not started.get("success"):
            return started
        run_id = str(started["run_id"])
        task = asyncio.create_task(
            service.execute_run(run_id, options),
            name=f"memory-dream:{run_id}",
        )
        _BACKGROUND_DREAM_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_DREAM_TASKS.discard)
        return {"success": True, "run_id": run_id, "status": "started"}

    @registry.tool(
        name="memory_dream_status",
        description="Return status and summary for a memory dream run.",
    )
    async def memory_dream_status(run_id: str) -> dict[str, Any]:
        return _service().status(run_id)

    @registry.tool(
        name="memory_dream_revert",
        description="Revert a completed memory dream run from snapshots.",
    )
    async def memory_dream_revert(run_id: str) -> dict[str, Any]:
        return await _service().revert(run_id)
