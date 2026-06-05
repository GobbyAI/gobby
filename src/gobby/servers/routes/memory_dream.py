"""HTTP routes for memory dream."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gobby.memory.dream.service import DreamRunOptions, MemoryDreamService

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class MemoryDreamRequest(BaseModel):
    dry_run: bool = False
    wait: bool = False
    skip_consolidation: bool = False
    memory_type: str | None = None
    project_id: str | None = None


def create_memory_dream_router(server: HTTPServer) -> APIRouter:
    """Create memory dream routes."""
    router = APIRouter(prefix="/memory", tags=["memory"])

    def _service() -> MemoryDreamService:
        dream_config = getattr(getattr(server.services.config, "memory", None), "dream", None)
        return MemoryDreamService(
            memory_manager=server.memory_manager,
            dream_config=dream_config,
            llm_service=getattr(server, "llm_service", None),
        )

    @router.post("/dream")
    async def memory_dream(request: MemoryDreamRequest) -> Any:
        service = _service()
        options = DreamRunOptions(
            dry_run=request.dry_run,
            skip_consolidation=request.skip_consolidation,
            memory_type=request.memory_type,
            project_id=request.project_id,
        )
        if request.wait:
            result = await service.run(options)
            status = 200 if result.get("success") else 500
            return JSONResponse(status_code=status, content=result)

        started = service.start(options)
        if not started.get("success"):
            return JSONResponse(status_code=400, content=started)
        run_id = str(started["run_id"])

        async def _background() -> None:
            result = await service.execute_run(run_id, options)
            if not result.get("success"):
                logger.warning("Background memory dream failed: %s", result.get("error"))

        task = asyncio.create_task(_background(), name=f"memory-dream:{run_id}")
        server._background_tasks.add(task)
        task.add_done_callback(server._background_tasks.discard)
        return JSONResponse(
            status_code=202,
            content={"success": True, "status": "started", "run_id": run_id},
        )

    @router.get("/dream/{run_id}")
    async def memory_dream_status(run_id: str) -> dict[str, Any]:
        result = _service().status(run_id)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @router.post("/dream/{run_id}/revert")
    async def memory_dream_revert(run_id: str) -> dict[str, Any]:
        result = await _service().revert(run_id)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    return router
