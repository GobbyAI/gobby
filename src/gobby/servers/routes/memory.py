"""
Memory routes for Gobby HTTP server.

Provides CRUD, search, and stats endpoints for the memory system.
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from gobby.utils.metrics import get_metrics_collector

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


# =============================================================================
# Request/Response models
# =============================================================================


class MemoryCreateRequest(BaseModel):
    """Request body for creating a memory."""

    content: str = Field(..., description="Memory content text")
    memory_type: str = Field(
        default="fact", description="Memory type (fact, preference, pattern, context)"
    )
    project_id: str | None = Field(default=None, description="Project ID to associate with")
    source_type: str = Field(default="user", description="Source type (user, session, inferred)")
    source_session_id: str | None = Field(default=None, description="Source session ID")
    tags: list[str] | None = Field(default=None, description="Tags for categorization")


class MemoryUpdateRequest(BaseModel):
    """Request body for updating a memory."""

    content: str | None = Field(default=None, description="New content text")
    tags: list[str] | None = Field(default=None, description="New tags")


# =============================================================================
# Router
# =============================================================================


def create_memory_router(server: "HTTPServer") -> APIRouter:
    """Create memory router with endpoints bound to server instance."""
    router = APIRouter(prefix="/api/memories", tags=["memories"])
    metrics = get_metrics_collector()

    @router.get("")
    def list_memories(
        project_id: str | None = Query(None, description="Filter by project ID"),
        memory_type: str | None = Query(None, description="Filter by memory type"),
        limit: int = Query(50, description="Maximum results"),
        offset: int = Query(0, description="Pagination offset"),
    ) -> dict[str, Any]:
        """List memories with optional filters."""
        metrics.inc_counter("http_requests_total")
        try:
            memories = server.memory_manager.list_memories(
                project_id=project_id,
                memory_type=memory_type,
                limit=limit,
                offset=offset,
            )
            total = server.memory_manager.count_memories(project_id=project_id)
            return {"memories": [m.to_dict() for m in memories], "total_memories": total}
        except Exception as e:
            logger.error(f"Failed to list memories: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("", status_code=201)
    async def create_memory(request_data: MemoryCreateRequest) -> Any:
        """Create a new memory."""
        metrics.inc_counter("http_requests_total")
        try:
            memory = await server.memory_manager.create_memory(
                content=request_data.content,
                memory_type=request_data.memory_type,
                project_id=request_data.project_id,
                source_type=request_data.source_type,
                source_session_id=request_data.source_session_id,
                tags=request_data.tags,
            )
            return memory.to_dict()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to create memory: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/search")
    async def search_memories(
        q: str = Query(..., description="Search query"),
        project_id: str | None = Query(None, description="Filter by project ID"),
        limit: int = Query(10, description="Maximum results"),
    ) -> dict[str, Any]:
        """Search memories by query."""
        metrics.inc_counter("http_requests_total")
        try:
            results = await server.memory_manager.search_memories(
                query=q,
                project_id=project_id,
                limit=limit,
            )
            return {
                "query": q,
                "results": [m.to_dict() for m in results],
                "count": len(results),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to search memories: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/stats")
    def memory_stats(
        project_id: str | None = Query(None, description="Filter by project ID"),
    ) -> Any:
        """Get memory statistics."""
        metrics.inc_counter("http_requests_total")
        try:
            return server.memory_manager.get_stats(project_id=project_id)
        except Exception as e:
            logger.error(f"Failed to get memory stats: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph/entities")
    async def entity_graph(
        limit: int = Query(500, description="Maximum entities to fetch"),
    ) -> dict[str, Any]:
        """Get Neo4j knowledge graph entities and relationships."""
        metrics.inc_counter("http_requests_total")
        if server.memory_manager is None or not getattr(
            server.memory_manager, "_neo4j_client", None
        ):
            raise HTTPException(status_code=404, detail="Neo4j not configured")
        try:
            result: dict[str, Any] | None = await server.memory_manager.get_entity_graph(
                limit=limit
            )
            if result is None:
                raise HTTPException(status_code=502, detail="Neo4j unreachable")
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get entity graph: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph/entities/{entity_name}/neighbors")
    async def entity_neighbors(entity_name: str) -> dict[str, Any]:
        """Get neighbors for a single Neo4j entity."""
        metrics.inc_counter("http_requests_total")
        if server.memory_manager is None or not getattr(
            server.memory_manager, "_neo4j_client", None
        ):
            raise HTTPException(status_code=404, detail="Neo4j not configured")
        try:
            result: dict[str, Any] | None = await server.memory_manager.get_entity_neighbors(
                entity_name
            )
            if result is None:
                raise HTTPException(status_code=502, detail="Neo4j unreachable")
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get entity neighbors: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/graph")
    def memory_graph(
        project_id: str | None = Query(None, description="Filter by project ID"),
        memory_limit: int = Query(200, description="Max memories (most recent first)"),
    ) -> dict[str, Any]:
        """Get memory graph data (memories + crossrefs) for visualization."""
        metrics.inc_counter("http_requests_total")
        try:
            memories = server.memory_manager.list_memories(
                project_id=project_id, limit=memory_limit
            )
            memory_ids = {m.id for m in memories}
            all_crossrefs = server.memory_manager.storage.get_all_crossrefs(
                project_id=project_id, limit=memory_limit * 10
            )
            crossrefs = [
                c for c in all_crossrefs if c.source_id in memory_ids and c.target_id in memory_ids
            ]
            return {
                "memories": [m.to_dict() for m in memories],
                "crossrefs": [c.to_dict() for c in crossrefs],
            }
        except Exception as e:
            logger.error(f"Failed to get memory graph: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/crossrefs/rebuild")
    async def rebuild_crossrefs(
        project_id: str | None = Query(None, description="Filter by project ID"),
    ) -> dict[str, Any]:
        """Rebuild crossrefs for all existing memories."""
        metrics.inc_counter("http_requests_total")
        try:
            memories = server.memory_manager.list_memories(project_id=project_id, limit=500)
            total_created = 0
            for memory in memories:
                try:
                    created = await server.memory_manager.rebuild_crossrefs_for_memory(memory)
                    total_created += created
                except Exception as e:
                    logger.warning(f"Crossref failed for {memory.id}: {e}")
            return {
                "memories_processed": len(memories),
                "crossrefs_created": total_created,
            }
        except Exception as e:
            logger.error(f"Failed to rebuild crossrefs: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/graph/rebuild")
    async def rebuild_knowledge_graph(
        project_id: str | None = Query(None, description="Filter by project ID"),
    ) -> dict[str, Any]:
        """Extract entities from all existing memories into the knowledge graph."""
        metrics.inc_counter("http_requests_total")
        try:
            kg = server.memory_manager.kg_service
            if not kg:
                raise HTTPException(
                    status_code=400,
                    detail="KnowledgeGraphService not initialized (requires Neo4j + LLM)",
                )
            memories = server.memory_manager.list_memories(project_id=project_id, limit=500)
            successful_count = 0
            errors = 0
            for memory in memories:
                try:
                    await kg.add_to_graph(memory.content)
                    successful_count += 1
                except Exception as e:
                    logger.warning(f"KG extraction failed for {memory.id}: {e}")
                    errors += 1
            return {
                "memories_processed": successful_count + errors,
                "memories_extracted": successful_count,
                "errors": errors,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to rebuild knowledge graph: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/{memory_id}")
    def get_memory(memory_id: str) -> Any:
        """Get a specific memory by ID."""
        metrics.inc_counter("http_requests_total")
        try:
            memory = server.memory_manager.get_memory(memory_id)
        except Exception as e:
            logger.error(f"Failed to get memory {memory_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

        if memory is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        return memory.to_dict()

    @router.put("/{memory_id}")
    async def update_memory(memory_id: str, request_data: MemoryUpdateRequest) -> Any:
        """Update an existing memory."""
        metrics.inc_counter("http_requests_total")
        try:
            memory = await server.memory_manager.update_memory(
                memory_id=memory_id,
                content=request_data.content,
                tags=request_data.tags,
            )
            return memory.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Failed to update memory {memory_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/{memory_id}")
    async def delete_memory(memory_id: str) -> dict[str, Any]:
        """Delete a memory."""
        metrics.inc_counter("http_requests_total")
        try:
            result = await server.memory_manager.delete_memory(memory_id)
        except Exception as e:
            logger.error(f"Failed to delete memory {memory_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e)) from e

        if not result:
            raise HTTPException(status_code=404, detail="Memory not found")
        return {"deleted": True, "id": memory_id}

    return router
