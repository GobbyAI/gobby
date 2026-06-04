"""Code index routes for graph operators and visualization."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gobby.code_index.context import CodeIndexGraphUnavailable, CodeIndexProjectNotFound
from gobby.code_index.gcode_gateway import (
    GcodeGatewayError,
    GcodeUnavailableError,
    GcodeVersionError,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)
_GRAPH_REQUEST_FAILED = "Code graph request failed"


class InvalidateIndexRequest(BaseModel):
    """Request body for POST /api/code-index/invalidate."""

    project_id: str


def _require_project_id(project_id: str | None) -> str:
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    return project_id


def _graph_http_exception(error: Exception) -> HTTPException:
    if isinstance(error, (CodeIndexGraphUnavailable, GcodeUnavailableError, GcodeVersionError)):
        return HTTPException(status_code=503, detail="Code graph not available")
    if isinstance(error, CodeIndexProjectNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, GcodeGatewayError):
        logger.exception("Code graph gateway request failed")
        return HTTPException(status_code=500, detail=_GRAPH_REQUEST_FAILED)
    logger.exception("Code graph request failed")
    return HTTPException(status_code=500, detail=_GRAPH_REQUEST_FAILED)


async def _run_db(server: HTTPServer, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run database work through the server bridge.

    Args:
        server: HTTP server that may expose an async ``run_db`` executor for database work.
        func: Synchronous callable to execute.
        *args: Positional arguments passed to ``func``.
        **kwargs: Keyword arguments passed to ``func``.

    Returns:
        The value returned by ``func``.

    If a custom ``server.run_db`` coroutine is available, it owns the sync/async
    handoff. Otherwise the callable runs in a worker thread so route handlers do
    not block the event loop.
    """
    runner = getattr(server, "run_db", None)
    if inspect.iscoroutinefunction(runner):
        return await runner(func, *args, **kwargs)
    return await asyncio.to_thread(func, *args, **kwargs)


def create_code_index_router(server: HTTPServer) -> APIRouter:
    """Create code-index routes."""
    router = APIRouter(prefix="/api/code-index", tags=["code-index"])

    @router.get("/graph")
    async def graph_overview(
        project_id: str | None = Query(None, description="Project ID"),
        limit: int = Query(200, description="Maximum files to include"),
    ) -> dict[str, Any]:
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is None:
            raise HTTPException(status_code=503, detail="Code graph not available")
        try:
            result = await code_indexer.graph_overview(
                _require_project_id(project_id),
                limit=limit,
            )
            return cast(dict[str, Any], result)
        except HTTPException:
            raise
        except (
            CodeIndexGraphUnavailable,
            CodeIndexProjectNotFound,
            GcodeGatewayError,
        ) as e:
            raise _graph_http_exception(e) from e
        except Exception as e:
            logger.exception(
                "Failed to load code graph overview",
                extra={
                    "error": str(e),
                    "context": {"route": "code_index", "operation": "graph_overview"},
                },
            )
            raise HTTPException(status_code=500, detail=_GRAPH_REQUEST_FAILED) from e

    @router.get("/graph/file/{file_path:path}")
    async def graph_file(
        file_path: str,
        project_id: str | None = Query(None, description="Project ID"),
    ) -> dict[str, Any]:
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is None:
            raise HTTPException(status_code=503, detail="Code graph not available")
        try:
            result = await code_indexer.graph_file(
                _require_project_id(project_id),
                file_path,
            )
            return cast(dict[str, Any], result)
        except HTTPException:
            raise
        except (
            CodeIndexGraphUnavailable,
            CodeIndexProjectNotFound,
            GcodeGatewayError,
        ) as e:
            raise _graph_http_exception(e) from e
        except Exception as e:
            logger.exception(
                "Failed to expand code graph file",
                extra={
                    "error": str(e),
                    "context": {
                        "route": "code_index",
                        "operation": "graph_file",
                        "file_path": file_path,
                    },
                },
            )
            raise HTTPException(status_code=500, detail=_GRAPH_REQUEST_FAILED) from e

    @router.get("/graph/symbol/{symbol_id}/neighbors")
    async def graph_symbol_neighbors(
        symbol_id: str,
        project_id: str | None = Query(None, description="Project ID"),
        limit: int = Query(50, description="Maximum neighbors to include"),
    ) -> dict[str, Any]:
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is None:
            raise HTTPException(status_code=503, detail="Code graph not available")
        try:
            result = await code_indexer.graph_symbol_neighbors(
                _require_project_id(project_id),
                symbol_id,
                limit=limit,
            )
            return cast(dict[str, Any], result)
        except HTTPException:
            raise
        except (
            CodeIndexGraphUnavailable,
            CodeIndexProjectNotFound,
            GcodeGatewayError,
        ) as e:
            raise _graph_http_exception(e) from e
        except Exception as e:
            logger.exception(
                "Failed to expand code graph symbol",
                extra={
                    "error": str(e),
                    "context": {
                        "route": "code_index",
                        "operation": "graph_symbol_neighbors",
                        "symbol_id": symbol_id,
                    },
                },
            )
            raise HTTPException(status_code=500, detail=_GRAPH_REQUEST_FAILED) from e

    @router.get("/graph/blast-radius")
    async def graph_blast_radius(
        project_id: str | None = Query(None, description="Project ID"),
        symbol_id: str | None = Query(None, description="Stable symbol or call-target ID"),
        file_path: str | None = Query(None, description="File path"),
        depth: int = Query(3, description="Maximum traversal depth"),
        limit: int = Query(100, description="Maximum affected nodes"),
    ) -> dict[str, Any]:
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is None:
            raise HTTPException(status_code=503, detail="Code graph not available")
        if bool(symbol_id) == bool(file_path):
            raise HTTPException(
                status_code=400, detail="Provide exactly one of symbol_id or file_path"
            )
        try:
            result = await code_indexer.graph_blast_radius(
                _require_project_id(project_id),
                symbol_id=symbol_id,
                file_path=file_path,
                depth=depth,
                limit=limit,
            )
            return cast(dict[str, Any], result)
        except HTTPException:
            raise
        except (
            CodeIndexGraphUnavailable,
            CodeIndexProjectNotFound,
            GcodeGatewayError,
        ) as e:
            raise _graph_http_exception(e) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception(
                "Failed to build blast radius graph",
                extra={
                    "error": str(e),
                    "context": {"route": "code_index", "operation": "graph_blast_radius"},
                },
            )
            raise HTTPException(status_code=500, detail=_GRAPH_REQUEST_FAILED) from e

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
            results = await _run_db(
                server,
                code_indexer.storage.search_symbols_fts,
                q,
                scoped_project,
                kind=None,
                file_path=None,
                limit=limit,
            )
            if not results:
                results = await _run_db(
                    server,
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
            logger.exception(
                "Failed to search code graph",
                extra={
                    "error": str(e),
                    "context": {"route": "code_index", "operation": "graph_search"},
                },
            )
            raise HTTPException(status_code=500, detail=_GRAPH_REQUEST_FAILED) from e

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
        except HTTPException:
            raise
        except (
            CodeIndexGraphUnavailable,
            CodeIndexProjectNotFound,
            GcodeGatewayError,
        ) as e:
            raise _graph_http_exception(e) from e
        except Exception as e:
            logger.exception(f"Failed to clear code graph for {scoped_project}")
            raise HTTPException(status_code=500, detail=_GRAPH_REQUEST_FAILED) from e
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
        except HTTPException:
            raise
        except (
            CodeIndexGraphUnavailable,
            CodeIndexProjectNotFound,
            GcodeGatewayError,
        ) as e:
            raise _graph_http_exception(e) from e
        except Exception as e:
            logger.exception(f"Failed to rebuild code graph for {scoped_project}")
            raise HTTPException(status_code=500, detail=_GRAPH_REQUEST_FAILED) from e
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

        stats = await _run_db(server, code_indexer.storage.get_project_stats, project_id)
        if stats is None:
            return JSONResponse(
                content={"status": "ok", "project_id": project_id, "note": "not indexed"},
            )

        await code_indexer.invalidate(project_id)
        return JSONResponse(content={"status": "ok", "project_id": project_id})

    @router.post("/codewiki/refresh", status_code=202)
    async def refresh_codewiki(
        request: Request,
        root_path: str = Query(..., description="Project root path"),
        project_id: str | None = Query(None, description="Project ID"),
        out_dir: str | None = Query(None, description="Optional codewiki output directory"),
        ai: str = Query("auto", description="AI route: auto, daemon, direct, or off"),
    ) -> dict[str, Any]:
        """Schedule a debounced codewiki refresh after post-commit indexing."""
        root_value = root_path.strip()
        if not root_value:
            raise HTTPException(status_code=400, detail="root_path is required")

        trigger = getattr(server.services, "codewiki_trigger", None)
        request_refresh = getattr(trigger, "request_refresh", None)
        if not callable(request_refresh):
            raise HTTPException(status_code=503, detail="Codewiki refresh trigger not available")

        try:
            accepted = bool(
                request_refresh(
                    root_path=root_value,
                    project_id=project_id,
                    out_dir=out_dir,
                    ai=ai,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except OSError as exc:
            if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            logger.exception(
                "Failed to schedule codewiki refresh",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "request_id": getattr(request.state, "request_id", None)
                    or request.headers.get("x-request-id"),
                    "root_path": root_value,
                    "project_id": project_id,
                    "ai": ai,
                    "error": str(exc),
                },
            )
            raise HTTPException(status_code=500, detail="Internal server error") from exc
        except Exception as exc:
            logger.exception(
                "Failed to schedule codewiki refresh",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "request_id": getattr(request.state, "request_id", None)
                    or request.headers.get("x-request-id"),
                    "root_path": root_value,
                    "project_id": project_id,
                    "ai": ai,
                    "error": str(exc),
                },
            )
            raise HTTPException(status_code=500, detail="Internal server error") from exc

        return {
            "accepted": accepted,
            "root_path": root_value,
            "project_id": project_id,
            "reason": None if accepted else "wiki.codewiki_on_commit disabled",
        }

    return router
