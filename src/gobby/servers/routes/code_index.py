"""Code index routes for Gobby HTTP server.

Provides endpoints for incremental indexing (git hooks) and status queries.
Bulk indexing and invalidation are handled directly by the CLI (`gobby index`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class IncrementalIndexRequest(BaseModel):
    """Request body for POST /api/code-index/incremental."""

    files: list[str]
    project_id: str = ""


class SessionStartIndexRequest(BaseModel):
    """Request body for POST /api/code-index/session-start."""

    project_id: str
    root_path: str
    session_id: str = ""


def create_code_index_router(server: HTTPServer) -> APIRouter:
    """Create code index router."""
    router = APIRouter(prefix="/api/code-index", tags=["code-index"])

    @router.post("/incremental")
    async def trigger_incremental_index(
        request: Request, body: IncrementalIndexRequest
    ) -> JSONResponse:
        """Called by git post-commit hook with list of changed files."""
        services = server.services
        code_indexer = getattr(services, "code_indexer", None)

        if code_indexer is None:
            return JSONResponse(
                status_code=503,
                content={"error": "Code indexer not available"},
            )

        if not body.files:
            return JSONResponse(
                status_code=400,
                content={"error": "No files provided for indexing"},
            )

        project_id = body.project_id or getattr(services, "project_id", "") or ""
        root_path = ""

        # Look up project root
        if project_id:
            project_stats = code_indexer.storage.get_project_stats(project_id)
            if project_stats:
                root_path = project_stats.root_path

        if not root_path:
            return JSONResponse(
                status_code=400,
                content={"error": "No root_path found for project"},
            )

        from gobby.code_index.watcher import handle_incremental_index

        result = await handle_incremental_index(
            indexer=code_indexer,
            project_id=project_id,
            root_path=root_path,
            changed_files=body.files,
        )

        return JSONResponse(content=result)

    @router.get("/status")
    async def index_status(project_id: str = "") -> JSONResponse:
        """Get indexing status for a project."""
        services = server.services
        code_indexer = getattr(services, "code_indexer", None)

        if code_indexer is None:
            return JSONResponse(
                status_code=503,
                content={"error": "Code indexer not available"},
            )

        pid = project_id or getattr(services, "project_id", "") or ""
        if not pid:
            projects = code_indexer.storage.list_indexed_projects()
            return JSONResponse(content={"projects": [p.to_dict() for p in projects]})

        stats = code_indexer.storage.get_project_stats(pid)
        if stats is None:
            return JSONResponse(content={"indexed": False, "project_id": pid})

        return JSONResponse(content={"indexed": True, **stats.to_dict()})

    @router.post("/session-start")
    async def trigger_session_start_index(
        request: Request, body: SessionStartIndexRequest
    ) -> JSONResponse:
        """Called by session handler to run incremental index on session start."""
        services = server.services
        code_indexer = getattr(services, "code_indexer", None)
        if code_indexer is None:
            return JSONResponse(status_code=503, content={"error": "Code indexer not available"})

        from gobby.code_index.watcher import handle_session_start_index

        result = await handle_session_start_index(
            indexer=code_indexer,
            project_id=body.project_id,
            root_path=body.root_path,
        )

        # Update code_index_available if indexing produced results
        if body.session_id and result.get("files_indexed", 0) > 0:
            try:
                from gobby.workflows.state_manager import SessionVariableManager

                db = code_indexer.storage.db
                sv_mgr = SessionVariableManager(db)
                sv_mgr.set_variable(body.session_id, "code_index_available", True)
            except Exception as e:
                logger.debug(f"Failed to set code_index_available after index: {e}")

        return JSONResponse(content=result)

    return router
