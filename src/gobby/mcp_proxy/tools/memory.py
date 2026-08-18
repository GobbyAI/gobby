"""
Internal MCP tools for Gobby Memory System.

Exposes functionality for:
- Creating memories (create_memory)
- Searching memories (search_memories)
- Deleting memories (delete_memory)
- Listing memories (list_memories)
- Getting memory details (get_memory)
- Updating memories (update_memory)
- Memory statistics (memory_stats)

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.memory_dream import register_memory_dream_tools
from gobby.mcp_proxy.tools.memory_recall import register_memory_recall_tool
from gobby.memory.digest import (
    build_turn_and_digest as _build_turn_and_digest,
)
from gobby.memory.manager import MemoryManager
from gobby.storage.memories import MemoryType, validate_memory_type
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.sync.memories import is_ephemeral_implementation_note

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.config.app import DaemonConfig
    from gobby.llm.service import LLMService
    from gobby.memory.dream.coordinator import MemoryDreamCoordinator

logger = logging.getLogger(__name__)

# Similarity at or above which create_memory supersedes the existing memory
# instead of leaving a permanent near-duplicate (dream no longer merges).
AUTO_SUPERSEDE_SIMILARITY = 0.9


# Helper to get current project context
def get_current_project_id() -> str | None:
    """Get the current project ID from context, or None if not in a project."""
    from gobby.utils.project_context import get_project_context

    ctx = get_project_context()
    if ctx and ctx.get("id"):
        return str(ctx["id"])
    return None


def _memory_owned_by_current_project(memory: Any) -> bool:
    if memory is None:
        return False
    current_project_id = get_current_project_id() or PERSONAL_PROJECT_ID
    return bool(memory.project_id == current_project_id)


def create_memory_registry(
    memory_manager_resolver: Callable[[], MemoryManager | None],
    llm_service_resolver: Callable[[], LLMService | None] | None = None,
    memory_backup_manager_resolver: Callable[[], Any | None] | None = None,
    session_manager: Any | None = None,
    startup_config: DaemonConfig | None = None,
    config_resolver: Callable[[], DaemonConfig | None] | None = None,
    dream_coordinator_resolver: Callable[[], MemoryDreamCoordinator | None] | None = None,
) -> InternalToolRegistry:
    """
    Create a memory tool registry with all memory-related tools.

    Args:
        memory_manager_resolver: per-call resolver for the current MemoryManager
        llm_service_resolver: per-call resolver for the current LLM service (optional)
        memory_backup_manager_resolver: per-call resolver for the current
            MemoryBackupManager (optional)
        session_manager: SessionManager for session lookups (optional)
        startup_config: DaemonConfig fallback before runtime readiness
        config_resolver: per-operation current DaemonConfig resolver
        dream_coordinator_resolver: resolves the daemon-owned dream coordinator
            for the memory_dream tools (optional)

    Returns:
        InternalToolRegistry with memory tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-memory",
        description="Memory management - create_memory, search_memories, delete_memory, get_related_memories",
    )

    def _memory_manager() -> MemoryManager:
        manager = memory_manager_resolver()
        if manager is None:
            raise RuntimeError("Memory services are unavailable in the current runtime epoch")
        return manager

    def _llm_service() -> LLMService | None:
        return llm_service_resolver() if llm_service_resolver is not None else None

    def _memory_backup_manager() -> Any | None:
        return (
            memory_backup_manager_resolver() if memory_backup_manager_resolver is not None else None
        )

    def _config() -> DaemonConfig | None:
        config = config_resolver() if config_resolver is not None else None
        return config if config is not None else startup_config

    @registry.tool(
        name="create_memory",
        description="Create a new memory. Returns similar existing memories to help detect duplicates.",
    )
    async def create_memory(
        content: str,
        memory_type: MemoryType | Literal["implementation_note"] = MemoryType.FACT,
        tags: list[str] | None = None,
        supersedes: list[str] | None = None,
        session_id: str | None = None,
        is_global: bool = False,
    ) -> dict[str, Any]:
        """
        Create a new memory.

        Args:
            content: The memory content to store
            memory_type: Type of memory (fact, preference, etc)
            tags: Optional list of tags
            supersedes: Up to 20 memory UUIDs to atomically soft-hide while recording
                ``supersedes:<id>`` provenance on the resulting memory. Use this for
                durable decisions, removals, and replacements. Near-duplicates
                (similarity >= 0.9) are superseded automatically and reported in
                ``auto_superseded``.
            session_id: Session ID that created this memory (accepts #N, N, UUID, or prefix)
        """
        try:
            from gobby.storage.memories_crud import normalize_supersedes

            supersedes_ids = normalize_supersedes(supersedes)
            if not supersedes_ids and is_ephemeral_implementation_note(
                {"content": content, "type": memory_type, "tags": tags or []}
            ):
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "ephemeral_implementation_note",
                }
            persisted_memory_type = (
                MemoryType.CONTEXT if memory_type == "implementation_note" else memory_type
            )
            canonical_memory_type = validate_memory_type(persisted_memory_type)

            project_id = get_current_project_id() or PERSONAL_PROJECT_ID

            # Resolve session_id to UUID before passing to storage layer
            # (memories.source_session_id has FK constraint on sessions.id)
            resolved_session_id: str | None = None
            if session_id:
                try:
                    from gobby.storage.session_resolution import resolve_session_reference

                    resolved_session_id = resolve_session_reference(
                        _memory_manager().db, session_id, project_id
                    )
                except Exception as e:
                    logger.warning("Could not resolve session_id '%s': %s", session_id, e)

            # Search for similar existing memories before creating: duplicates
            # surface in the result, and near-duplicates are superseded
            # atomically by the create (dream no longer merges, so unhandled
            # duplicates are permanent).
            similar_existing: list[dict[str, Any]] = []
            auto_superseded: list[dict[str, Any]] = []
            try:
                similar = await _memory_manager().search_memories(
                    query=content,
                    project_id=project_id,
                    limit=4,
                    caller="mcp_proxy.memory.create_memory.similar_existing",
                )
                for m in similar:
                    similarity = getattr(m, "similarity", None)
                    similar_existing.append(
                        {
                            "id": m.id,
                            "content": m.content,
                            "similarity": similarity,
                        }
                    )
                    if (
                        isinstance(similarity, int | float)
                        and not isinstance(similarity, bool)
                        and float(similarity) >= AUTO_SUPERSEDE_SIMILARITY
                        and m.id not in supersedes_ids
                    ):
                        auto_superseded.append({"id": m.id, "similarity": float(similarity)})
                similar_existing = similar_existing[:3]
                if auto_superseded:
                    supersedes_ids = normalize_supersedes(
                        [*supersedes_ids, *(entry["id"] for entry in auto_superseded)]
                    )
            except Exception as e:
                auto_superseded = []
                logger.debug(
                    "Similarity search failed during memory creation (project_id=%s): %s",
                    project_id,
                    e,
                    exc_info=True,
                )

            memory = await _memory_manager().create_memory(
                content=content,
                memory_type=canonical_memory_type,
                project_id=project_id,
                tags=tags,
                supersedes=supersedes_ids,
                source_type="agent",
                source_session_id=resolved_session_id,
                is_global=is_global,
            )

            result: dict[str, Any] = {
                "success": True,
                "memory": {
                    "id": memory.id,
                    "project_id": memory.project_id,
                    "is_global": memory.is_global,
                },
                "similar_existing": similar_existing,
            }
            if auto_superseded:
                result["auto_superseded"] = auto_superseded
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="search_memories",
        description="Search memories based on query and filters. Supports tag-based filtering.",
    )
    async def search_memories(
        query: str | None = None,
        limit: int = 10,
        min_score: float = 0.0,
        memory_type: MemoryType | None = None,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Search memories based on query and filters.

        Args:
            query: Search query string
            limit: Maximum number of memories to return
            min_score: Optional minimum semantic similarity score (0.0-1.0).
            tags_all: Memory must have ALL of these tags
            tags_any: Memory must have at least ONE of these tags
            tags_none: Memory must have NONE of these tags
        """
        try:
            from uuid import uuid4

            from gobby.utils.session_context import get_current_session_id

            effective_min_score = min_score if min_score > 0 else 0.0
            # Joinable correlation id (contract §2): threads the signal event,
            # the returned payload, and any downstream injection outcome.
            recall_request_id = str(uuid4())
            current_project_id = get_current_project_id() or PERSONAL_PROJECT_ID
            canonical_memory_type = (
                validate_memory_type(memory_type) if memory_type is not None else None
            )

            # Fetch extra candidates so we can report diagnostics when
            # nothing passes the threshold.
            candidates = await _memory_manager().search_memories(
                query=query,
                project_id=current_project_id,
                limit=limit * 2 if effective_min_score > 0 else limit,
                min_score=None,  # no threshold — filter below
                memory_type=canonical_memory_type,
                tags_all=tags_all,
                tags_any=tags_any,
                tags_none=tags_none,
                session_id=get_current_session_id(),
                recall_request_id=recall_request_id,
                caller="mcp_proxy.memory.search_memories",
            )

            # Split by threshold
            above: list[dict[str, Any]] = []
            below_count = 0
            max_score_seen = 0.0
            for m in candidates:
                similarity = getattr(m, "similarity", None)
                threshold_score = similarity or 0.0
                max_score_seen = max(max_score_seen, threshold_score)
                if effective_min_score > 0 and threshold_score < effective_min_score:
                    below_count += 1
                    continue
                if len(above) < limit:
                    above.append(
                        {
                            "id": m.id,
                            "content": m.content,
                            "type": m.memory_type,
                            "created_at": m.created_at,
                            "tags": m.tags,
                            "project_id": m.project_id,
                            "is_global": m.is_global,
                            "similarity": similarity,
                            "search_via": getattr(m, "search_via", None),
                            "ranking_score": getattr(m, "ranking_score", None),
                            "raw_semantic_score": getattr(m, "raw_semantic_score", None),
                            "temporal_decay_factor": getattr(m, "temporal_decay_factor", None),
                            "ranking_mode": getattr(m, "ranking_mode", None),
                        }
                    )

            result: dict[str, Any] = {
                "success": True,
                "memories": above,
                "recall_request_id": recall_request_id,
                "project_id": current_project_id,
            }
            if not above and below_count > 0:
                result["below_threshold_count"] = below_count
                result["max_score_seen"] = round(max_score_seen, 4)
                result["threshold"] = effective_min_score

            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="delete_memory",
        description="Delete a memory by ID.",
    )
    async def delete_memory(memory_id: str) -> dict[str, Any]:
        """
        Delete a memory by ID.

        Args:
            memory_id: The ID of the memory to delete
        """
        try:
            success = await _memory_manager().delete_memory_scoped(
                memory_id,
                get_current_project_id() or PERSONAL_PROJECT_ID,
            )
            if success:
                return {"success": True}
            else:
                return {"success": False, "error": f"Memory {memory_id} not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="restore_memory",
        description="Restore a soft-hidden (dream-flagged) memory back to active visibility.",
    )
    async def restore_memory(memory_id: str) -> dict[str, Any]:
        """
        Restore a dream-flagged (soft-hidden) memory to active visibility.

        Hidden memories are excluded from agent reads and recall; restoring one
        makes it visible again. Raises if the memory does not exist.

        Args:
            memory_id: The ID of the memory to restore
        """
        try:
            memory = _memory_manager().get_memory(memory_id, visibility="all")
            if memory is None or not _memory_owned_by_current_project(memory):
                return {"success": False, "error": f"Memory {memory_id} not found"}
            await asyncio.to_thread(_memory_manager().restore_memory, memory_id)
            await _memory_manager().restore_memory_indices(
                memory.id,
                memory.content,
                memory.project_id,
                memory.is_global,
                getattr(memory.memory_type, "value", memory.memory_type),
            )
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="promote_memory_to_global",
        description="Promote a project-scoped memory to global memory scope.",
    )
    async def promote_memory_to_global(memory_id: str) -> dict[str, Any]:
        """
        Promote a memory to global scope.

        Args:
            memory_id: The ID of the memory to promote
        """
        try:
            existing = _memory_manager().get_memory(memory_id, visibility="all")
            if not _memory_owned_by_current_project(existing):
                return {"success": False, "error": f"Memory {memory_id} not found"}
            memory = await _memory_manager().promote_memory(memory_id)
            return {
                "success": True,
                "memory": {
                    "id": memory.id,
                    "project_id": memory.project_id,
                    "is_global": memory.is_global,
                    "updated_at": memory.updated_at,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="demote_memory_from_global",
        description="Restrict a globally visible memory to its owning project.",
    )
    async def demote_memory_from_global(memory_id: str) -> dict[str, Any]:
        try:
            existing = _memory_manager().get_memory(memory_id, visibility="all")
            if not _memory_owned_by_current_project(existing):
                return {"success": False, "error": f"Memory {memory_id} not found"}
            memory = await _memory_manager().demote_memory(memory_id)
            return {"success": True, "memory": memory.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="move_memory",
        description="Move memory ownership to another concrete project.",
    )
    async def move_memory(memory_id: str, new_project_id: str) -> dict[str, Any]:
        try:
            existing = _memory_manager().get_memory(memory_id, visibility="all")
            if not _memory_owned_by_current_project(existing):
                return {"success": False, "error": f"Memory {memory_id} not found"}
            memory = await _memory_manager().move_memory(memory_id, new_project_id)
            return {"success": True, "memory": memory.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="list_memories",
        description="List all memories with optional filtering. Supports tag-based filtering.",
    )
    def list_memories(
        memory_type: MemoryType | None = None,
        limit: int = 50,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        List all memories with optional filtering.

        Args:
            memory_type: Filter by memory type (fact, preference, pattern, context)
            limit: Maximum number of memories to return
            tags_all: Memory must have ALL of these tags
            tags_any: Memory must have at least ONE of these tags
            tags_none: Memory must have NONE of these tags
        """
        try:
            canonical_memory_type = (
                validate_memory_type(memory_type) if memory_type is not None else None
            )
            memories = _memory_manager().list_memories(
                project_id=get_current_project_id() or PERSONAL_PROJECT_ID,
                memory_type=canonical_memory_type,
                limit=limit,
                tags_all=tags_all,
                tags_any=tags_any,
                tags_none=tags_none,
            )
            return {
                "success": True,
                "memories": [
                    {
                        "id": m.id,
                        "content": m.content,
                        "type": m.memory_type,
                        "created_at": m.created_at,
                        "tags": m.tags,
                        "project_id": m.project_id,
                        "is_global": m.is_global,
                    }
                    for m in memories
                ],
                "count": len(memories),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_memory",
        description="Get details of a specific memory by ID.",
    )
    def get_memory(memory_id: str) -> dict[str, Any]:
        """
        Get details of a specific memory.

        Args:
            memory_id: The ID of the memory to retrieve
        """
        try:
            memory = _memory_manager().get_memory(memory_id, project_id=get_current_project_id())
            if memory:
                return {
                    "success": True,
                    "memory": {
                        "id": memory.id,
                        "content": memory.content,
                        "type": memory.memory_type,
                        "created_at": memory.created_at,
                        "updated_at": memory.updated_at,
                        "project_id": memory.project_id,
                        "is_global": memory.is_global,
                        "source_type": memory.source_type,
                        "access_count": memory.access_count,
                        "tags": memory.tags,
                    },
                }
            else:
                return {"success": False, "error": f"Memory {memory_id} not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_related_memories",
        description="Get memories related to a specific memory via cross-references.",
    )
    async def get_related_memories(
        memory_id: str,
        limit: int = 5,
        min_similarity: float = 0.0,
    ) -> dict[str, Any]:
        """
        Get memories linked to a specific memory via cross-references.

        Cross-references are automatically created based on semantic similarity
        when memories are stored (if auto_crossref is enabled in config).

        Args:
            memory_id: The ID of the memory to find related memories for
            limit: Maximum number of related memories to return
            min_similarity: Minimum similarity threshold (0.0-1.0)
        """
        try:
            memories = await _memory_manager().get_related(
                memory_id=memory_id,
                limit=limit,
                min_similarity=min_similarity,
                project_id=get_current_project_id(),
            )
            return {
                "success": True,
                "memory_id": memory_id,
                "related": [
                    {
                        "id": m.id,
                        "content": m.content,
                        "type": m.memory_type,
                        "created_at": m.created_at,
                        "tags": m.tags,
                    }
                    for m in memories
                ],
                "count": len(memories),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="update_memory",
        description="Update an existing memory's content or tags.",
    )
    async def update_memory(
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Update an existing memory.

        Args:
            memory_id: The ID of the memory to update
            content: New content (optional)
            tags: New list of tags (optional)
        """
        try:
            memory = await _memory_manager().update_memory_scoped(
                memory_id=memory_id,
                project_id=get_current_project_id() or PERSONAL_PROJECT_ID,
                content=content,
                tags=tags,
            )
            return {
                "success": True,
                "memory": {
                    "id": memory.id,
                    "updated_at": memory.updated_at,
                    "project_id": memory.project_id,
                    "is_global": memory.is_global,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="memory_stats",
        description="Get statistics about the memory system.",
    )
    async def memory_stats() -> dict[str, Any]:
        """
        Get statistics about stored memories.
        """
        try:
            stats = await _memory_manager().get_stats(project_id=get_current_project_id())
            return {"success": True, "stats": stats}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="search_knowledge_graph",
        description="Search the knowledge graph backend for entities matching a query.",
    )
    async def search_knowledge_graph(
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """
        Search the knowledge graph for entities matching a query.

        Args:
            query: Search query string
            limit: Maximum number of results to return
        """
        try:
            kg_service = _memory_manager().kg_service
            if not kg_service:
                return {"success": True, "results": []}

            results = await kg_service.search_graph(
                query,
                limit=limit,
                project_id=get_current_project_id(),
                include_global=True,
            )
            return {"success": True, "results": results}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="recluster_knowledge_graph_entities",
        description="Run offline HDBSCAN clustering over knowledge-graph entity embeddings and persist _Entity.cluster_id labels.",
    )
    async def recluster_knowledge_graph_entities(
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Recluster knowledge graph entities for a project.

        Args:
            project_id: Optional project ID. Defaults to the current project.
        """
        try:
            kg_service = _memory_manager().kg_service
            if not kg_service:
                return {"success": False, "error": "Knowledge graph service not available"}

            effective_project_id = (
                project_id if project_id is not None else get_current_project_id()
            )
            result = await kg_service.recluster_entities(project_id=effective_project_id)
            return {
                "success": True,
                "project_id": result.project_id,
                "entity_count": result.entity_count,
                "valid_entity_count": result.valid_entity_count,
                "clustered_entity_count": result.clustered_entity_count,
                "cluster_count": result.cluster_count,
                "noise_count": result.noise_count,
                "invalid_count": result.invalid_count,
                "clusters": result.cluster_summaries,
                "quality_metrics": result.quality_metrics,
            }
        except ImportError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="densify_knowledge_graph_cooccurrence",
        description=(
            "Bulk-materialize derived CO_OCCURS edges over the existing knowledge graph "
            "from MENTIONED_IN structure and stored entity embeddings (no LLM). "
            "Idempotent and batched; safe to rerun."
        ),
    )
    async def densify_knowledge_graph_cooccurrence(
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Retrofit CO_OCCURS support edges onto a graph built before
        materialize_cooccurrence was enabled.

        Args:
            project_id: Optional project ID. Defaults to the current project.
        """
        try:
            kg_service = _memory_manager().kg_service
            if not kg_service:
                return {"success": False, "error": "Knowledge graph service not available"}

            effective_project_id = (
                project_id if project_id is not None else get_current_project_id()
            )
            result = await kg_service.densify_cooccurrence(project_id=effective_project_id)
            return {"success": True, **asdict(result)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="rebuild_crossrefs",
        description="Rebuild cross-references between all memories based on semantic similarity. Creates edges for the 2D memory graph.",
    )
    async def rebuild_crossrefs(
        project_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """
        Rebuild cross-references for all existing memories.

        Uses vector similarity to find related memories and create links.
        These links power the 2D memory graph visualization.

        Args:
            project_id: Optional project ID to filter memories
            limit: Maximum number of memories to process (default 500)
        """
        try:
            memories = _memory_manager().list_memories(project_id=project_id, limit=limit)
            total_created = 0
            for i, memory in enumerate(memories):
                try:
                    created = await _memory_manager().rebuild_crossrefs_for_memory(memory)
                    total_created += created
                except Exception as e:
                    logger.warning("Crossref failed for %s: %s", memory.id, e)
                if i % 10 == 9:
                    await asyncio.sleep(0)
            return {
                "success": True,
                "memories_processed": len(memories),
                "crossrefs_created": total_created,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="rebuild_knowledge_graph",
        description="Extract entities and relationships from all memories into the FalkorDB knowledge graph. Powers the 3D graph visualization.",
    )
    async def rebuild_knowledge_graph(
        project_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """
        Rebuild the knowledge graph from all existing memories.

        Extracts entities and relationships using LLM and stores them in FalkorDB.
        This powers the 3D knowledge graph visualization.

        Args:
            project_id: Optional project ID to filter memories
            limit: Max memories to process (default 500)
        """
        try:
            result = await _memory_manager().rebuild_knowledge_graph(
                project_id=project_id,
                limit=limit,
            )
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="reindex_embeddings",
        description="Regenerate embedding vectors for all stored memories. Useful after changing embedding models or for initial setup.",
    )
    async def reindex_embeddings() -> dict[str, Any]:
        """
        Regenerate embeddings for all stored memories.
        """
        try:
            return await _memory_manager().reindex_embeddings()
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ─── Sync & extraction tools (thin wrappers around workflow actions) ───

    @registry.tool(
        name="restore_memories",
        description="Restore memories from .gobby/memories.jsonl when backup timestamps win.",
    )
    async def restore_memories() -> dict[str, Any]:
        """Non-destructively restore memories from the configured JSONL backup."""
        backup_manager = _memory_backup_manager()
        if not backup_manager:
            return {"success": False, "error": "Memory backup manager not available"}
        try:
            count = await backup_manager.restore()
            return {"success": True, "restored": count}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="backup_memories",
        description="Write current live memories to .gobby/memories.jsonl.",
    )
    async def backup_memories() -> dict[str, Any]:
        """Write a deterministic JSONL backup from the hub database."""
        backup_manager = _memory_backup_manager()
        if not backup_manager:
            return {"success": False, "error": "Memory backup manager not available"}
        try:
            project_id = get_current_project_id()
            count = await backup_manager.backup(project_id=project_id)
            return {"success": True, "backed_up": count}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # NOTE: DB rules invoke this tool at turn-end and Codex turn-start catch-up boundaries.
    # It is NOT called directly from Python code. Keep both lifecycle rules in sync with this API.
    @registry.tool(
        name="build_turn_and_digest",
        description=(
            "Build a detailed turn record, append it to the session digest, and synthesize "
            "a title. Lifecycle rules invoke it at turn-end and for Codex turn-start catch-up."
        ),
    )
    async def build_turn_and_digest_tool(
        session_id: str = "",
        prompt_text: str | None = None,
        catch_up: bool = False,
    ) -> dict[str, Any]:
        """
        Build turn record and append to digest after agent response.

        Reads the last user/assistant exchange from the transcript,
        generates a structured turn record via LLM, appends it to the
        session's rolling digest, and synthesizes a title via
        ``build_turn_and_digest``.

        Args:
            session_id: Platform session ID (injected by dispatch layer)
            prompt_text: Optional user prompt (usually None for stop events)
            catch_up: Drain a bounded undigested backlog batch at turn start,
                excluding the active turn
        """
        if not session_id:
            return {"success": False, "error": "session_id is required"}
        try:
            result = await _build_turn_and_digest(
                memory_manager=_memory_manager(),
                session_manager=session_manager,
                session_id=session_id,
                prompt_text=prompt_text,
                llm_service=_llm_service(),
                config=_config(),
                catch_up=catch_up,
            )
            if result is None:
                return {"success": True, "skipped": True, "reason": "disabled or no content"}
            if "error" in result:
                return {"success": False, "error": result["error"]}
            return {"success": True, **result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    register_memory_recall_tool(
        registry,
        memory_manager_resolver,
        llm_service_resolver=_llm_service,
        config_resolver=lambda: (
            config.memory_recall if (config := _config()) is not None else None
        ),
    )
    register_memory_dream_tools(
        registry,
        coordinator_resolver=dream_coordinator_resolver or (lambda: None),
        get_project_id=get_current_project_id,
    )

    return registry
