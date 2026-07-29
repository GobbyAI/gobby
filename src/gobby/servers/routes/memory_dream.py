"""HTTP routes for memory dream."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gobby.memory.dream.coordinator import DREAM_RUN_CONFLICT, MemoryDreamCoordinator
from gobby.memory.dream.options import DreamRunOptions
from gobby.servers.responses import JSONResponse

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class MemoryDreamRequest(BaseModel):
    dry_run: bool = False
    skip_consolidation: bool = False
    memory_type: str | None = None
    project_id: str | None = None
    full_sweep: bool = False


def _http_status(result: dict[str, Any]) -> int:
    """Map the coordinator trigger contract onto HTTP status codes."""
    if result.get("success"):
        # Newly admitted runs are 202; coalescing onto an active run is 200.
        return 200 if result.get("coalesced") else 202
    if result.get("error_code") == DREAM_RUN_CONFLICT:
        return 409
    # A failed launch already left a terminal run row; anything else is a bad
    # request (disabled feature or invalid options).
    return 500 if result.get("status") == "failed" else 400


def create_memory_dream_router(server: HTTPServer) -> APIRouter:
    """Create memory dream routes."""
    router = APIRouter(prefix="/memory", tags=["memory"])

    def _coordinator() -> MemoryDreamCoordinator:
        coordinator: MemoryDreamCoordinator | None = getattr(
            server.services, "memory_dream_coordinator", None
        )
        if coordinator is None:
            raise HTTPException(status_code=503, detail="memory dream coordinator is unavailable")
        return coordinator

    @router.post("/dream")
    async def memory_dream(request: MemoryDreamRequest) -> Any:
        coordinator = _coordinator()
        if request.project_id is None:
            # Unscoped trigger → round-robin every scope with due memories.
            result = await coordinator.trigger_all_due_projects(
                dry_run=request.dry_run,
                skip_consolidation=request.skip_consolidation,
                memory_type=request.memory_type,
                full_sweep=request.full_sweep,
            )
        else:
            result = await coordinator.trigger(
                DreamRunOptions(
                    dry_run=request.dry_run,
                    skip_consolidation=request.skip_consolidation,
                    memory_type=request.memory_type,
                    project_id=request.project_id,
                    full_sweep=request.full_sweep,
                )
            )
        return JSONResponse(status_code=_http_status(result), content=result)

    @router.get("/dream/{run_id}")
    async def memory_dream_status(run_id: str) -> dict[str, Any]:
        result = await _coordinator().service.status(run_id)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    @router.post("/dream/{run_id}/revert")
    async def memory_dream_revert(run_id: str) -> dict[str, Any]:
        result = await _coordinator().service.revert(run_id)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error"))
        return result

    return router
