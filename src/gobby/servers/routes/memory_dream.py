"""HTTP routes for memory dream."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gobby.memory.dream.options import DreamRunOptions
from gobby.memory.dream.service import MemoryDreamService
from gobby.servers.responses import JSONResponse

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class MemoryDreamRequest(BaseModel):
    dry_run: bool = False
    wait: bool = False
    skip_consolidation: bool = False
    memory_type: str | None = None
    project_id: str | None = None
    full_sweep: bool = False


def create_memory_dream_router(server: HTTPServer) -> APIRouter:
    """Create memory dream routes."""
    router = APIRouter(prefix="/memory", tags=["memory"])

    def _service() -> MemoryDreamService:
        if server.memory_manager is None:
            raise HTTPException(status_code=503, detail="memory manager is unavailable")
        config = getattr(server.services, "config", None)
        memory_config = getattr(config, "memory", None)
        dream_config = getattr(memory_config, "dream", None)
        if dream_config is None:
            raise HTTPException(status_code=503, detail="memory dream config is unavailable")
        return MemoryDreamService(
            memory_manager=server.memory_manager,
            dream_config=dream_config,
            llm_service=getattr(server, "llm_service", None),
            daemon_config=config,
            current_project_id=getattr(server.services, "project_id", None),
        )

    @router.post("/dream")
    async def memory_dream(request: MemoryDreamRequest) -> Any:
        service = _service()
        if request.project_id is None:
            # Unscoped trigger → sweep every project with due memories, each
            # judged against its own truth digest, and return an aggregate.
            if request.wait:
                result = await service.run_all_due_projects(
                    dry_run=request.dry_run,
                    skip_consolidation=request.skip_consolidation,
                    memory_type=request.memory_type,
                    full_sweep=request.full_sweep,
                )
                status = 200 if result.get("success") else 500
                return JSONResponse(status_code=status, content=result)
            started = await service.start_all_due_projects_async(
                dry_run=request.dry_run,
                skip_consolidation=request.skip_consolidation,
                memory_type=request.memory_type,
                full_sweep=request.full_sweep,
            )
            if not started.get("success"):
                return JSONResponse(status_code=400, content=started)
            run_id = str(started["run_id"])

            async def _aggregate_background() -> None:
                try:
                    result = await service.execute_all_due_projects_run(
                        run_id,
                        dry_run=request.dry_run,
                        skip_consolidation=request.skip_consolidation,
                        memory_type=request.memory_type,
                        full_sweep=request.full_sweep,
                    )
                    if not result.get("success"):
                        logger.warning("Background memory dream failed: %s", result.get("error"))
                except Exception as exc:
                    service.record_run_failure(run_id, str(exc))
                    logger.warning("Background memory dream failed: %s", exc, exc_info=True)

            task = asyncio.create_task(_aggregate_background(), name=f"memory-dream:{run_id}")
            server.register_background_task(task)
            return JSONResponse(
                status_code=202,
                content={"success": True, "status": "started", "run_id": run_id},
            )

        options = DreamRunOptions(
            dry_run=request.dry_run,
            skip_consolidation=request.skip_consolidation,
            memory_type=request.memory_type,
            project_id=request.project_id,
            full_sweep=request.full_sweep,
        )
        if request.wait:
            result = await service.run(options)
            status = 200 if result.get("success") else 500
            return JSONResponse(status_code=status, content=result)

        started = await service.start_async(options)
        if not started.get("success"):
            return JSONResponse(status_code=400, content=started)
        run_id = str(started["run_id"])

        async def _background() -> None:
            try:
                result = await service.execute_run(run_id, options)
                if not result.get("success"):
                    logger.warning("Background memory dream failed: %s", result.get("error"))
            except Exception as exc:
                service.record_run_failure(run_id, str(exc))
                logger.warning("Background memory dream failed: %s", exc, exc_info=True)

        task = asyncio.create_task(_background(), name=f"memory-dream:{run_id}")
        server.register_background_task(task)
        return JSONResponse(
            status_code=202,
            content={"success": True, "status": "started", "run_id": run_id},
        )

    @router.get("/dream/{run_id}")
    async def memory_dream_status(run_id: str) -> dict[str, Any]:
        result = await _service().status(run_id)
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
