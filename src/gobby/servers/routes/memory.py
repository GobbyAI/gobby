"""
Memory routes for Gobby HTTP server.

Provides CRUD, search, and stats endpoints for the memory system.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gobby.servers.responses import JSONResponse
from gobby.storage.memories import Memory, MemoryType, Visibility
from gobby.storage.memories_crud import normalize_supersedes
from gobby.storage.memories_scope import ALL_MEMORIES, MemoryScope
from gobby.storage.projects import PERSONAL_PROJECT_ID

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

_BATCH_SIZE = 500
_DEFAULT_ENTITY_GRAPH_LIMIT = 500
_DEFAULT_RELATIONSHIP_GRAPH_LIMIT = 2000


def _bounded_graph_limit(requested: int, configured: object, *, default: int) -> int:
    """Apply the positive operator-configured ceiling; zero requests use that ceiling."""
    ceiling = configured if isinstance(configured, int) and not isinstance(configured, bool) else 0
    if ceiling <= 0:
        ceiling = default
    return ceiling if requested == 0 else min(requested, ceiling)


def _fetch_all_memories(server: "HTTPServer", project_id: str | None = None) -> list[Memory]:
    """Fetch all memories using pagination (no hardcoded limit)."""
    all_memories: list[Memory] = []
    offset = 0
    while True:
        batch = server.memory_manager.list_memories(
            project_id=project_id, limit=_BATCH_SIZE, offset=offset
        )
        if not batch:
            break
        all_memories.extend(batch)
        if len(batch) < _BATCH_SIZE:
            break
        offset += _BATCH_SIZE
    return all_memories


def _require_falkordb_memory_manager(server: "HTTPServer") -> Any:
    memory_manager = getattr(server, "memory_manager", None)
    if memory_manager is None or getattr(memory_manager, "_falkor_client", None) is None:
        raise HTTPException(status_code=404, detail="FalkorDB not configured")
    return memory_manager


# =============================================================================
# Request/Response models
# =============================================================================


class MemoryCreateRequest(BaseModel):
    """Request body for creating a memory."""

    content: str = Field(..., description="Memory content text")
    rationale: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Why a future, unrelated session should be served this memory",
    )
    source_task_id: str | None = Field(
        default=None,
        description="Optional task UUID that produced this memory",
    )
    created_by_agent: str | None = Field(
        default=None,
        description="Optional agent or CLI source that wrote this memory",
    )
    memory_type: MemoryType = Field(
        default=MemoryType.FACT,
        description="Memory type (fact, preference, pattern, context)",
    )
    project_id: str | None = Field(default=None, description="Project ID to associate with")
    is_global: bool = Field(default=False, description="Whether the memory is globally visible")
    source_type: str = Field(
        default="user",
        description="Source type: 'user' (human-requested) or 'agent' (agent-captured)",
    )
    source_session_id: str | None = Field(default=None, description="Source session ID")
    tags: list[str] | None = Field(default=None, description="Tags for categorization")
    supersedes: list[str] | None = Field(
        default=None,
        description="Memory UUIDs atomically replaced by this memory",
    )

    @field_validator("supersedes")
    @classmethod
    def validate_supersedes(cls, value: list[str] | None) -> list[str]:
        return normalize_supersedes(value)


class MemoryUpdateRequest(BaseModel):
    """Request body for updating a memory."""

    content: str | None = Field(default=None, description="New content text")
    tags: list[str] | None = Field(default=None, description="New tags")
    memory_type: MemoryType | None = Field(default=None, description="New memory type")


class MemoryMoveRequest(BaseModel):
    """Request body for moving a memory to another owning project."""

    new_project_id: str = Field(..., description="New owning project ID")


class MemoryPromoteRequest(BaseModel):
    """Empty request body that rejects unsupported promotion arguments."""

    model_config = ConfigDict(extra="forbid")


def _current_project_id(server: "HTTPServer") -> str | None:
    from gobby.utils.project_context import get_project_context

    try:
        project_ctx = get_project_context()
    except (LookupError, OSError):
        logger.debug("Failed to load current project context", exc_info=True)
        project_ctx = None
    if project_ctx and project_ctx.get("id"):
        return str(project_ctx["id"])
    return cast(str | None, getattr(getattr(server, "services", None), "project_id", None))


def _ensure_memory_in_current_project(server: "HTTPServer", memory_id: str) -> None:
    memory = server.memory_manager.get_memory(memory_id, visibility="all")
    if memory is None:
        raise ValueError(f"Memory {memory_id} not found")
    current_project_id = _current_project_id(server) or PERSONAL_PROJECT_ID
    if not memory.is_global and memory.project_id != current_project_id:
        raise ValueError(f"Memory {memory_id} not found")


# =============================================================================
# Router
# =============================================================================


def create_memory_router(server: "HTTPServer") -> APIRouter:
    """Create memory router with endpoints bound to server instance."""
    router = APIRouter(prefix="/api/memories", tags=["memories"])
    rebuild_state: dict[str, Any] = {
        "job_id": None,
        "status": "idle",
        "project_id": None,
        "started_at": None,
        "updated_at": None,
        "completed_at": None,
        "memories_total": 0,
        "memories_completed": 0,
        "memories_marked_processed": 0,
        "status_counts": {},
        "errors": 0,
        "failed_memories": [],
        "result": None,
        "error": None,
    }
    rebuild_state_lock = asyncio.Lock()
    rebuild_start_lock = asyncio.Lock()

    def _rebuild_snapshot() -> dict[str, Any]:
        result = rebuild_state.get("result")
        return {
            **rebuild_state,
            "status_counts": dict(rebuild_state.get("status_counts") or {}),
            "failed_memories": list(rebuild_state.get("failed_memories") or []),
            "result": dict(result) if isinstance(result, dict) else result,
        }

    async def _update_rebuild_state(**updates: Any) -> dict[str, Any]:
        async with rebuild_state_lock:
            rebuild_state.update(updates)
            rebuild_state["updated_at"] = datetime.now(UTC).isoformat()
            return _rebuild_snapshot()

    async def _get_rebuild_state() -> dict[str, Any]:
        async with rebuild_state_lock:
            return _rebuild_snapshot()

    async def _begin_rebuild(project_id: str | None) -> tuple[str | None, dict[str, Any]]:
        async with rebuild_start_lock:
            current = await _get_rebuild_state()
            if current.get("status") == "running":
                current["started"] = False
                current["already_running"] = True
                return None, current

            job_id = str(uuid4())
            started_at = datetime.now(UTC).isoformat()
            snapshot = await _update_rebuild_state(
                job_id=job_id,
                status="running",
                project_id=project_id,
                started_at=started_at,
                completed_at=None,
                memories_total=0,
                memories_completed=0,
                memories_marked_processed=0,
                status_counts={},
                errors=0,
                failed_memories=[],
                result=None,
                error=None,
            )
            return job_id, snapshot

    async def _execute_graph_rebuild(job_id: str, project_id: str | None) -> dict[str, Any]:
        async def _on_progress(progress: dict[str, Any]) -> None:
            await _update_rebuild_state(
                job_id=job_id,
                status="running",
                project_id=project_id,
                memories_total=progress.get("memories_total", 0),
                memories_completed=progress.get("memories_completed", 0),
                memories_marked_processed=progress.get("memories_marked_processed", 0),
                status_counts=progress.get("status_counts", {}),
                errors=progress.get("errors", 0),
                failed_memories=progress.get("failed_memories", []),
                error=None,
            )

        try:
            result = await server.memory_manager.rebuild_knowledge_graph(
                project_id=project_id,
                progress_callback=_on_progress,
            )
            await _update_rebuild_state(
                job_id=job_id,
                status="completed",
                project_id=project_id,
                completed_at=datetime.now(UTC).isoformat(),
                memories_total=result.get("memories_processed", 0),
                memories_completed=result.get("memories_processed", 0),
                memories_marked_processed=result.get("memories_marked_processed", 0),
                status_counts=result.get("status_counts", {}),
                errors=result.get("errors", 0),
                failed_memories=result.get("failed_memories", []),
                result=result,
                error=None,
            )
            logger.info("Background knowledge graph rebuild complete: %s", result)
            return cast(dict[str, Any], result)
        except Exception as e:
            await _update_rebuild_state(
                job_id=job_id,
                status="failed",
                project_id=project_id,
                completed_at=datetime.now(UTC).isoformat(),
                error=str(e),
            )
            logger.exception("Background knowledge graph rebuild failed: %s", e)
            raise

    async def _run_graph_rebuild(job_id: str, project_id: str | None) -> None:
        await _execute_graph_rebuild(job_id, project_id)

    @router.get("")
    def list_memories(
        project_id: str | None = Query(None, description="Filter by project ID"),
        memory_type: MemoryType | None = Query(None, description="Filter by memory type"),
        limit: int = Query(50, description="Maximum results"),
        offset: int = Query(0, description="Pagination offset"),
        visibility: Visibility = Query(
            "active",
            description=(
                "Which memories to return: 'active' (default, dream-visible), "
                "'hidden' (dream-flagged for review/deletion), or 'all'"
            ),
        ),
    ) -> dict[str, Any]:
        """List memories with optional filters.

        ``visibility`` selects the dream soft-delete scope and is applied to both
        the page and its total so the count always matches the filtered rows.
        """
        try:
            memories = server.memory_manager.list_memories(
                project_id=project_id,
                memory_type=memory_type,
                limit=limit,
                offset=offset,
                visibility=visibility,
            )
            total = server.memory_manager.count_memories(
                project_id=project_id,
                memory_type=memory_type,
                visibility=visibility,
            )
            return {"memories": [m.to_dict() for m in memories], "total_memories": total}
        except Exception as e:
            logger.error("Failed to list memories: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("", status_code=201)
    async def create_memory(request_data: MemoryCreateRequest) -> Any:
        """Create a new memory."""
        try:
            memory = await server.memory_manager.create_memory(
                content=request_data.content,
                memory_type=request_data.memory_type,
                project_id=request_data.project_id,
                is_global=request_data.is_global,
                source_type=request_data.source_type,
                source_session_id=request_data.source_session_id,
                tags=request_data.tags,
                supersedes=request_data.supersedes,
                rationale=request_data.rationale,
                source_task_id=request_data.source_task_id,
                created_by_agent=request_data.created_by_agent,
            )
            return memory.to_dict()
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to create memory: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/search")
    async def search_memories(
        q: str = Query(..., description="Search query"),
        project_id: str | None = Query(None, description="Filter by project ID"),
        memory_type: MemoryType | None = Query(None, description="Filter by memory type"),
        limit: int = Query(10, description="Maximum results"),
    ) -> dict[str, Any]:
        """Search memories by query."""
        try:
            results = await server.memory_manager.search_memories(
                query=q,
                project_id=project_id,
                memory_type=memory_type,
                limit=limit,
                caller="http.memory.search",
            )
            return {
                "query": q,
                "results": [m.to_dict() for m in results],
                "count": len(results),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to search memories: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/stats")
    async def memory_stats(
        project_id: str | None = Query(None, description="Filter by project ID"),
    ) -> Any:
        """Get memory statistics."""
        try:
            return await server.memory_manager.get_stats(project_id=project_id)
        except Exception as e:
            logger.error("Failed to get memory stats: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph/entities")
    async def entity_graph(
        limit: int = Query(
            _DEFAULT_ENTITY_GRAPH_LIMIT,
            ge=0,
            description="Maximum entities to fetch (0 = configured ceiling)",
        ),
        relationship_limit: int = Query(
            _DEFAULT_RELATIONSHIP_GRAPH_LIMIT,
            ge=0,
            description="Maximum relationships to fetch (0 = configured ceiling)",
        ),
        project_id: str | None = Query(None, description="Filter by project ID"),
    ) -> dict[str, Any]:
        """Get FalkorDB knowledge graph entities and relationships, most recent first."""
        memory_manager = _require_falkordb_memory_manager(server)
        ui_config = getattr(
            getattr(getattr(server, "services", None), "config", None),
            "ui",
            None,
        )
        limit = _bounded_graph_limit(
            limit,
            getattr(ui_config, "knowledge_graph_limit", None),
            default=_DEFAULT_ENTITY_GRAPH_LIMIT,
        )
        relationship_limit = _bounded_graph_limit(
            relationship_limit,
            getattr(ui_config, "knowledge_graph_relationship_limit", None),
            default=_DEFAULT_RELATIONSHIP_GRAPH_LIMIT,
        )
        try:
            result: dict[str, Any] | None = await memory_manager.get_entity_graph(
                limit=limit,
                relationship_limit=relationship_limit,
                project_id=project_id,
            )
            if result is None:
                raise HTTPException(status_code=502, detail="FalkorDB unreachable")
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get entity graph: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph/entities/{entity_key}/neighbors")
    async def entity_neighbors(
        entity_key: str,
        project_id: str | None = Query(None, description="Filter by project ID"),
    ) -> dict[str, Any]:
        """Get neighbors for a single FalkorDB entity."""
        memory_manager = _require_falkordb_memory_manager(server)
        try:
            result: dict[str, Any] | None = await memory_manager.get_entity_neighbors(
                entity_key,
                project_id=project_id,
            )
            if result is None:
                raise HTTPException(status_code=502, detail="FalkorDB unreachable")
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get entity neighbors: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph/counts")
    async def graph_counts(
        project_id: str | None = Query(None, description="Filter by project ID"),
    ) -> dict[str, Any]:
        """Get actual FalkorDB knowledge-graph counts."""
        memory_manager = _require_falkordb_memory_manager(server)
        try:
            result: dict[str, Any] = await memory_manager.get_knowledge_graph_counts(
                project_id=project_id,
            )
            if not result.get("success", True):
                raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get knowledge graph counts: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph")
    def memory_graph(
        project_id: str | None = Query(None, description="Filter by project ID"),
        memory_limit: int = Query(
            200, ge=1, le=1000, description="Max memories (most recent first)"
        ),
    ) -> dict[str, Any]:
        """Get memory graph data (memories + crossrefs) for visualization."""
        try:
            memories = server.memory_manager.list_memories(
                project_id=project_id, limit=memory_limit
            )
            memory_ids = {m.id for m in memories}
            scope = MemoryScope.project_visible(project_id) if project_id else ALL_MEMORIES
            all_crossrefs = server.memory_manager.storage.get_all_crossrefs(
                scope=scope,
                limit=memory_limit * 10,
            )
            crossrefs = [
                c for c in all_crossrefs if c.source_id in memory_ids and c.target_id in memory_ids
            ]
            return {
                "memories": [m.to_dict() for m in memories],
                "crossrefs": [c.to_dict() for c in crossrefs],
            }
        except Exception as e:
            logger.error("Failed to get memory graph: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/crossrefs/rebuild")
    async def rebuild_crossrefs(
        project_id: str | None = Query(None, description="Filter by project ID"),
    ) -> dict[str, Any]:
        """Rebuild crossrefs for all existing memories."""
        try:
            memories = _fetch_all_memories(server, project_id)
            total_created = 0
            for memory in memories:
                try:
                    created = await server.memory_manager.rebuild_crossrefs_for_memory(memory)
                    total_created += created
                except Exception as e:
                    logger.warning("Crossref failed for %s: %s", memory.id, e)
            return {
                "memories_processed": len(memories),
                "crossrefs_created": total_created,
            }
        except Exception as e:
            logger.error("Failed to rebuild crossrefs: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/graph/clear")
    async def clear_knowledge_graph(
        project_id: str | None = Query(None, description="Filter by project ID"),
    ) -> dict[str, Any]:
        """Clear only the FalkorDB knowledge-graph projection."""
        try:
            result = await server.memory_manager.clear_knowledge_graph(project_id=project_id)
            if not result.get("success", False):
                raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
            return cast(dict[str, Any], result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to clear knowledge graph: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/graph/rebuild", response_model=None)
    async def rebuild_knowledge_graph(
        project_id: str | None = Query(None, description="Filter by project ID"),
        background: bool = Query(False, description="Run rebuild in background and return a job"),
    ) -> Any:
        """Extract entities from all existing memories into the knowledge graph."""
        try:
            if background:
                job_id, snapshot = await _begin_rebuild(project_id)
                if job_id is None:
                    return JSONResponse(status_code=202, content=snapshot)

                task = asyncio.create_task(
                    _run_graph_rebuild(job_id, project_id),
                    name=f"memory-kg-rebuild:{job_id}",
                )
                server._background_tasks.add(task)
                task.add_done_callback(server._background_tasks.discard)

                snapshot["started"] = True
                return JSONResponse(status_code=202, content=snapshot)

            job_id, snapshot = await _begin_rebuild(project_id)
            if job_id is None:
                return JSONResponse(status_code=202, content=snapshot)

            result = await _execute_graph_rebuild(job_id, project_id)
            if not result.get("success", False):
                raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to rebuild knowledge graph: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph/rebuild/status")
    async def rebuild_knowledge_graph_status(
        job_id: str | None = Query(None, description="Specific rebuild job ID to inspect"),
    ) -> dict[str, Any]:
        """Get status for the most recent background knowledge-graph rebuild."""
        snapshot = await _get_rebuild_state()
        current_job_id = snapshot.get("job_id")
        if job_id and current_job_id != job_id:
            raise HTTPException(status_code=404, detail=f"Rebuild job not found: {job_id}")
        return snapshot

    @router.post("/reconcile")
    async def reconcile_memory_stores(
        dry_run: bool = Query(False, description="Report orphans without deleting"),
    ) -> dict[str, Any]:
        """Reconcile Qdrant and FalkorDB with the hub database source of truth."""
        try:
            return cast(
                dict[str, Any], await server.memory_manager.reconcile_stores(dry_run=dry_run)
            )
        except Exception as e:
            logger.error("Failed to reconcile memory stores: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/embeddings/reindex")
    async def reindex_embeddings(
        project_id: str | None = Query(
            None, description="Project ID (optional, scopes to project)"
        ),
    ) -> dict[str, Any]:
        """Regenerate embedding vectors for stored memories."""
        try:
            result = await server.memory_manager.reindex_embeddings(project_id=project_id)
            return cast(dict[str, Any], result)
        except Exception as e:
            logger.error("Failed to reindex embeddings: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/invalidate")
    async def invalidate_memories(
        project_id: str | None = Query(None, description="Project ID (optional, omit for global)"),
    ) -> dict[str, Any]:
        """Clear all secondary memory indices and start background rebuild.

        Clears Qdrant vectors, FalkorDB graph, and crossrefs immediately.
        Rebuild runs in the background — the response returns as soon as
        indices are cleared.
        """
        try:
            report: dict[str, Any] = await server.memory_manager.clear_indices(
                project_id=project_id
            )
        except Exception as e:
            logger.error("Failed to clear memory indices: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

        # Fire off rebuild as a background task
        async def _background_rebuild() -> None:
            try:
                result = await server.memory_manager.rebuild_indices(project_id=project_id)
                logger.info("Background rebuild complete: %s", result)
            except Exception as e:
                logger.exception("Background rebuild failed: %s", e)

        task = asyncio.create_task(_background_rebuild())
        server._background_tasks.add(task)
        task.add_done_callback(server._background_tasks.discard)

        report["rebuild_status"] = "started"
        return report

    @router.get("/{memory_id}")
    def get_memory(
        memory_id: str,
        project_id: str | None = Query(None, description="Project ID for scoping"),
    ) -> Any:
        """Get a specific memory by ID, optionally scoped to a project."""
        try:
            memory = server.memory_manager.get_memory(memory_id, project_id=project_id)
        except Exception as e:
            logger.error("Failed to get memory %s: %s", memory_id, e)
            raise HTTPException(status_code=500, detail=str(e)) from e

        if memory is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        return memory.to_dict()

    @router.put("/{memory_id}")
    async def update_memory(memory_id: str, request_data: MemoryUpdateRequest) -> Any:
        """Update an existing memory."""
        try:
            memory = await server.memory_manager.update_memory(
                memory_id=memory_id,
                content=request_data.content,
                tags=request_data.tags,
                memory_type=request_data.memory_type,
            )
            return memory.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error("Failed to update memory %s: %s", memory_id, e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{memory_id}/restore")
    async def restore_memory(memory_id: str) -> Any:
        """Restore a soft-hidden (dream-flagged) memory to active visibility.

        Returns the restored memory so callers can refresh from the response.
        """
        try:
            _ensure_memory_in_current_project(server, memory_id)
            hidden_memory = server.memory_manager.get_memory(memory_id, visibility="all")
            server.memory_manager.restore_memory(memory_id)
            await server.memory_manager.restore_memory_indices(
                hidden_memory.id,
                hidden_memory.content,
                hidden_memory.project_id,
                hidden_memory.is_global,
                hidden_memory.memory_type.value,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Failed to restore memory %s", memory_id)
            raise HTTPException(status_code=500, detail=str(e)) from e

        memory = server.memory_manager.get_memory(memory_id)
        if memory is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        return memory.to_dict()

    @router.post("/{memory_id}/promote")
    async def promote_memory(
        memory_id: str,
        _request_data: MemoryPromoteRequest | None = None,
    ) -> Any:
        """Promote a project memory to global scope."""
        try:
            _ensure_memory_in_current_project(server, memory_id)
            memory = await server.memory_manager.promote_memory(memory_id)
            return memory.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Failed to promote memory %s", memory_id)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{memory_id}/demote")
    async def demote_memory(memory_id: str) -> Any:
        """Remove global visibility without changing memory ownership."""
        try:
            _ensure_memory_in_current_project(server, memory_id)
            memory = await server.memory_manager.demote_memory(memory_id)
            return memory.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Failed to demote memory %s", memory_id)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{memory_id}/move")
    async def move_memory(memory_id: str, request_data: MemoryMoveRequest) -> Any:
        """Move a memory to a new owning project without changing visibility."""
        try:
            _ensure_memory_in_current_project(server, memory_id)
            memory = await server.memory_manager.move_memory(
                memory_id,
                request_data.new_project_id,
            )
            return memory.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Failed to move memory %s", memory_id)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/{memory_id}")
    async def delete_memory(memory_id: str) -> dict[str, Any]:
        """Delete a memory."""
        try:
            result = await server.memory_manager.delete_memory(memory_id)
        except Exception as e:
            logger.error("Failed to delete memory %s: %s", memory_id, e)
            raise HTTPException(status_code=500, detail=str(e)) from e

        if not result:
            raise HTTPException(status_code=404, detail="Memory not found")
        return {"deleted": True, "id": memory_id}

    return router
