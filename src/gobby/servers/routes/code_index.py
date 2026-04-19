"""Code index routes for graph operators and visualization."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class InvalidateIndexRequest(BaseModel):
    """Request body for POST /api/code-index/invalidate."""

    project_id: str


def _require_project_id(project_id: str | None) -> str:
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    return project_id


def create_code_index_router(server: HTTPServer) -> APIRouter:
    """Create code-index routes."""
    router = APIRouter(prefix="/api/code-index", tags=["code-index"])

    @router.get("/graph")
    async def graph_overview(
        project_id: str | None = Query(None, description="Project ID"),
        limit: int = Query(200, description="Maximum files to include"),
    ) -> dict[str, Any]:
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is None or code_indexer.graph is None or not code_indexer.graph.available:
            raise HTTPException(status_code=503, detail="Code graph not available")
        try:
            result = await code_indexer.graph.get_file_graph(
                _require_project_id(project_id),
                limit=limit,
            )
            return cast(dict[str, Any], result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to load code graph overview: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph/file/{file_path:path}")
    async def graph_file(
        file_path: str,
        project_id: str | None = Query(None, description="Project ID"),
    ) -> dict[str, Any]:
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is None or code_indexer.graph is None or not code_indexer.graph.available:
            raise HTTPException(status_code=503, detail="Code graph not available")
        try:
            result = await code_indexer.graph.get_file_symbols(
                file_path,
                _require_project_id(project_id),
            )
            return cast(dict[str, Any], result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to expand code graph file {file_path}: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph/symbol/{symbol_id}/neighbors")
    async def graph_symbol_neighbors(
        symbol_id: str,
        project_id: str | None = Query(None, description="Project ID"),
        limit: int = Query(50, description="Maximum neighbors to include"),
    ) -> dict[str, Any]:
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is None or code_indexer.graph is None or not code_indexer.graph.available:
            raise HTTPException(status_code=503, detail="Code graph not available")
        try:
            result = await code_indexer.graph.get_symbol_neighbors(
                symbol_id,
                _require_project_id(project_id),
                limit=limit,
            )
            return cast(dict[str, Any], result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to expand code graph symbol {symbol_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph/blast-radius")
    async def graph_blast_radius(
        project_id: str | None = Query(None, description="Project ID"),
        symbol_id: str | None = Query(None, description="Stable symbol or call-target ID"),
        file_path: str | None = Query(None, description="File path"),
        depth: int = Query(3, description="Maximum traversal depth"),
        limit: int = Query(100, description="Maximum affected nodes"),
    ) -> dict[str, Any]:
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is None or code_indexer.graph is None or not code_indexer.graph.available:
            raise HTTPException(status_code=503, detail="Code graph not available")
        if bool(symbol_id) == bool(file_path):
            raise HTTPException(
                status_code=400, detail="Provide exactly one of symbol_id or file_path"
            )
        try:
            result = await code_indexer.graph.get_blast_radius_graph(
                symbol_id=symbol_id,
                file_path=file_path,
                project_id=_require_project_id(project_id),
                depth=depth,
                limit=limit,
            )
            return cast(dict[str, Any], result)
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Failed to build blast radius graph: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph/search")
    async def graph_search(
        project_id: str | None = Query(None, description="Project ID"),
        q: str = Query(..., description="Search query"),
        limit: int = Query(25, description="Maximum results"),
    ) -> dict[str, Any]:
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is None:
            raise HTTPException(status_code=503, detail="Code indexer not available")

        scoped_project = _require_project_id(project_id)
        try:
            results = await asyncio.to_thread(
                code_indexer.storage.search_symbols_fts,
                q,
                scoped_project,
                kind=None,
                file_path=None,
                limit=limit,
            )
            if not results:
                results = await asyncio.to_thread(
                    code_indexer.storage.search_symbols_by_name,
                    q,
                    scoped_project,
                    kind=None,
                    file_path=None,
                    limit=limit,
                )
            return {
                "results": [
                    {
                        "id": symbol.id,
                        "name": symbol.name,
                        "type": symbol.kind,
                        "kind": symbol.kind,
                        "file_path": symbol.file_path,
                        "line_start": symbol.line_start,
                        "signature": symbol.signature,
                    }
                    for symbol in results
                ]
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to search code graph: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/graph/clear")
    async def clear_graph(
        project_id: str | None = Query(None, description="Project ID"),
    ) -> dict[str, Any]:
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is None:
            raise HTTPException(status_code=503, detail="Code indexer not available")
        scoped_project = _require_project_id(project_id)
        try:
            result = await code_indexer.clear_graph(scoped_project)
        except Exception as e:
            logger.exception(f"Failed to clear code graph for {scoped_project}")
            raise HTTPException(status_code=500, detail=str(e)) from e
        if not result.get("success", False):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
        return cast(dict[str, Any], result)

    @router.post("/graph/rebuild")
    async def rebuild_graph(
        project_id: str | None = Query(None, description="Project ID"),
        limit: int = Query(10_000, description="Maximum indexed files to replay"),
    ) -> dict[str, Any]:
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is None:
            raise HTTPException(status_code=503, detail="Code indexer not available")
        scoped_project = _require_project_id(project_id)
        try:
            result = await code_indexer.rebuild_graph(scoped_project, limit=limit)
        except Exception as e:
            logger.exception(f"Failed to rebuild code graph for {scoped_project}")
            raise HTTPException(status_code=500, detail=str(e)) from e
        if not result.get("success", False):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
        return cast(dict[str, Any], result)

    @router.post("/invalidate")
    async def invalidate_index(body: InvalidateIndexRequest) -> JSONResponse:
        """Clear all index data for a project. Called by gcode invalidate."""
        services = server.services
        code_indexer = getattr(services, "code_indexer", None)

        if code_indexer is None:
            return JSONResponse(
                status_code=503,
                content={"error": "Code indexer not available"},
            )

        project_id = body.project_id
        if not project_id:
            return JSONResponse(
                status_code=400,
                content={"error": "project_id is required"},
            )

        stats = await asyncio.to_thread(code_indexer.storage.get_project_stats, project_id)
        if stats is None:
            return JSONResponse(
                content={"status": "ok", "project_id": project_id, "note": "not indexed"},
            )

        await code_indexer.invalidate(project_id)
        return JSONResponse(content={"status": "ok", "project_id": project_id})

    return router
