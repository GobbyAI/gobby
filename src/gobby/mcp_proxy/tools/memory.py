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
from typing import TYPE_CHECKING, Any, Protocol

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.memory_dream import register_memory_dream_tools
from gobby.memory.digest import (
    bootstrap_session_title as _bootstrap_session_title,
)
from gobby.memory.digest import (
    build_turn_and_digest as _build_turn_and_digest,
)
from gobby.memory.digest import (
    memory_sync_export,
    memory_sync_import,
)
from gobby.memory.manager import MemoryManager
from gobby.sync.memories import is_ephemeral_implementation_note

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.llm.service import LLMService

logger = logging.getLogger(__name__)


class SupportsTaskDecomposition(Protocol):
    def create_task_with_decomposition(
        self,
        *,
        project_id: str,
        title: str,
        **kwargs: Any,
    ) -> dict[str, Any]: ...


# Helper to get current project context
def get_current_project_id() -> str | None:
    """Get the current project ID from context, or None if not in a project."""
    from gobby.utils.project_context import get_project_context

    ctx = get_project_context()
    if ctx and ctx.get("id"):
        return str(ctx["id"])
    return None


def _memory_allowed_in_current_project(memory: Any) -> bool:
    memory_project_id = getattr(memory, "project_id", None)
    current_project_id = get_current_project_id()
    return memory_project_id is None or (
        current_project_id is not None and memory_project_id == current_project_id
    )


def _speculative_memory_task_title(content: str) -> str | None:
    """Return a planning task title for narrow implementation-proposal memories."""
    normalized = " ".join(content.lower().split())
    has_session_start_pattern = (
        "sessionstart" in normalized
        and "ensure_session_activation" in normalized
        and "helper" in normalized
        and "replay raw sessionstart side effects" in normalized
    )
    has_proposal_language = any(
        marker in normalized
        for marker in (
            "proposed",
            "proposal",
            "call an idempotent",
            "call a helper",
        )
    )
    if has_session_start_pattern and has_proposal_language:
        return "gobby-session-start-reconciliation-proposal"
    return None


def _redirect_speculative_memory_to_task(
    *,
    memory_manager: MemoryManager,
    task_manager: SupportsTaskDecomposition | None,
    title: str,
    content: str,
    project_id: str | None,
    source_session_id: str | None,
) -> dict[str, Any]:
    from gobby.storage.projects import PERSONAL_PROJECT_ID
    from gobby.storage.session_tasks import SessionTaskManager
    from gobby.storage.tasks import LocalTaskManager

    manager = task_manager or LocalTaskManager(memory_manager.db)
    result = manager.create_task_with_decomposition(
        project_id=project_id or PERSONAL_PROJECT_ID,
        title=title,
        description=(
            "Redirected from gobby-memory.create_memory because this content is a "
            "speculative implementation proposal rather than durable memory.\n\n"
            f"{content}"
        ),
        priority=3,
        task_type="research_spike",
        labels=["memory-redirect", "planning-note"],
        category="planning",
        created_in_session_id=source_session_id,
    )
    task = result.get("task") if isinstance(result, dict) else None
    if not isinstance(task, dict) or not task.get("id"):
        raise RuntimeError("Speculative memory redirection did not create a task.")

    if source_session_id:
        try:
            SessionTaskManager(memory_manager.db).link_task(
                source_session_id, task["id"], "created"
            )
        except Exception as e:
            logger.debug("Failed to link redirected memory task to session: %s", e)

    seq_num = task.get("seq_num")
    return {
        "id": task["id"],
        "ref": f"#{seq_num}" if seq_num else task["id"],
    }


def create_memory_registry(
    memory_manager: MemoryManager,
    llm_service: LLMService | None = None,
    memory_sync_manager: Any | None = None,
    session_manager: Any | None = None,
    config: DaemonConfig | None = None,
    task_manager: SupportsTaskDecomposition | None = None,
) -> InternalToolRegistry:
    """
    Create a memory tool registry with all memory-related tools.

    Args:
        memory_manager: MemoryManager instance
        llm_service: LLM service for AI-powered extraction (optional)
        memory_sync_manager: MemoryBackupManager for sync import/export (optional)
        session_manager: SessionManager for session lookups (optional)
        config: DaemonConfig carrying digest feature routing config (optional)

    Returns:
        InternalToolRegistry with memory tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-memory",
        description="Memory management - create_memory, search_memories, delete_memory, get_related_memories",
    )

    @registry.tool(
        name="create_memory",
        description="Create a new memory. Returns similar existing memories to help detect duplicates.",
    )
    async def create_memory(
        content: str,
        memory_type: str = "fact",
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a new memory.

        Args:
            content: The memory content to store
            memory_type: Type of memory (fact, preference, etc)
            tags: Optional list of tags
            session_id: Session ID that created this memory (accepts #N, N, UUID, or prefix)
        """
        try:
            if is_ephemeral_implementation_note(
                {"content": content, "type": memory_type, "tags": tags or []}
            ):
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "ephemeral_implementation_note",
                }

            project_id = get_current_project_id()

            # Resolve session_id to UUID before passing to storage layer
            # (memories.source_session_id has FK constraint on sessions.id)
            resolved_session_id: str | None = None
            if session_id:
                try:
                    from gobby.storage.session_resolution import resolve_session_reference

                    resolved_session_id = resolve_session_reference(
                        memory_manager.db, session_id, project_id
                    )
                except Exception as e:
                    logger.warning(f"Could not resolve session_id '{session_id}': {e}")

            redirected_task_title = _speculative_memory_task_title(content)
            if redirected_task_title:
                try:
                    task = _redirect_speculative_memory_to_task(
                        memory_manager=memory_manager,
                        task_manager=task_manager,
                        title=redirected_task_title,
                        content=content,
                        project_id=project_id,
                        source_session_id=resolved_session_id,
                    )
                    return {
                        "success": True,
                        "redirected_to_task_note": True,
                        "task_id": task["id"],
                        "task_ref": task["ref"],
                    }
                except Exception as e:
                    logger.warning(
                        "Speculative memory redirection failed; storing original memory: %s",
                        e,
                        exc_info=True,
                    )

            memory = await memory_manager.create_memory(
                content=content,
                memory_type=memory_type,
                project_id=project_id,
                tags=tags,
                source_type="agent",
                source_session_id=resolved_session_id,
            )

            # Search for similar existing memories to surface potential duplicates
            similar_existing: list[dict[str, Any]] = []
            try:
                similar = await memory_manager.search_memories(
                    query=content,
                    project_id=project_id,
                    limit=4,  # fetch 4 since the new memory itself may appear
                    caller="mcp_proxy.memory.create_memory.similar_existing",
                )
                for m in similar:
                    if m.id != memory.id:
                        similar_existing.append(
                            {
                                "id": m.id,
                                "content": m.content,
                                "similarity": getattr(m, "similarity", None),
                            }
                        )
                similar_existing = similar_existing[:3]
            except Exception as e:
                logger.debug(
                    f"Similarity search failed during memory creation (project_id={project_id}, memory_id={memory.id}): {e}",
                    exc_info=True,
                )

            return {
                "success": True,
                "memory": {
                    "id": memory.id,
                },
                "similar_existing": similar_existing,
            }
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
            current_project_id = get_current_project_id()

            # Fetch extra candidates so we can report diagnostics when
            # nothing passes the threshold.
            candidates = await memory_manager.search_memories(
                query=query,
                project_id=current_project_id,
                limit=limit * 2 if effective_min_score > 0 else limit,
                min_score=None,  # no threshold — filter below
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
            success = await memory_manager.delete_memory_scoped(
                memory_id,
                get_current_project_id(),
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
            memory = memory_manager.get_memory(memory_id, visibility="all")
            if not _memory_allowed_in_current_project(memory):
                return {"success": False, "error": f"Memory {memory_id} not found"}
            await asyncio.to_thread(memory_manager.restore_memory, memory_id)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="promote_memory_to_global",
        description="Promote a project-scoped memory to global memory scope.",
    )
    async def promote_memory_to_global(
        memory_id: str,
        target_project_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Promote a memory to global scope.

        Args:
            memory_id: The ID of the memory to promote
            target_project_id: Reserved for future rescope support; must be null
        """
        if target_project_id is not None:
            return {
                "success": False,
                "error": "Only promote-to-global is supported.",
            }
        try:
            existing = memory_manager.get_memory(memory_id, visibility="all")
            if not _memory_allowed_in_current_project(existing):
                return {"success": False, "error": f"Memory {memory_id} not found"}
            memory = await memory_manager.rescope_memory(memory_id, None)
            return {
                "success": True,
                "memory": {
                    "id": memory.id,
                    "project_id": memory.project_id,
                    "updated_at": memory.updated_at,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="list_memories",
        description="List all memories with optional filtering. Supports tag-based filtering.",
    )
    def list_memories(
        memory_type: str | None = None,
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
            memories = memory_manager.list_memories(
                project_id=get_current_project_id(),
                memory_type=memory_type,
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
            memory = memory_manager.get_memory(memory_id, project_id=get_current_project_id())
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
            memories = await memory_manager.get_related(
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
            memory = await memory_manager.update_memory_scoped(
                memory_id=memory_id,
                project_id=get_current_project_id(),
                content=content,
                tags=tags,
            )
            return {
                "success": True,
                "memory": {
                    "id": memory.id,
                    "updated_at": memory.updated_at,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="memory_stats",
        description="Get statistics about the memory system.",
    )
    def memory_stats() -> dict[str, Any]:
        """
        Get statistics about stored memories.
        """
        try:
            stats = memory_manager.get_stats(project_id=get_current_project_id())
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
            kg_service = memory_manager.kg_service
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
            kg_service = memory_manager.kg_service
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
            kg_service = memory_manager.kg_service
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
            memories = memory_manager.list_memories(project_id=project_id, limit=limit)
            total_created = 0
            for i, memory in enumerate(memories):
                try:
                    created = await memory_manager.rebuild_crossrefs_for_memory(memory)
                    total_created += created
                except Exception as e:
                    logger.warning(f"Crossref failed for {memory.id}: {e}")
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
            result = await memory_manager.rebuild_knowledge_graph(
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
            return await memory_manager.reindex_embeddings()
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ─── Sync & extraction tools (thin wrappers around workflow actions) ───

    @registry.tool(
        name="bootstrap_session_title",
        description="Set a local heuristic session title from the first meaningful user prompt. Fired by a turn_start rule before the first completed turn.",
    )
    async def bootstrap_session_title_tool(
        session_id: str = "",
        prompt_text: str | None = None,
    ) -> dict[str, Any]:
        """
        Bootstrap a session title without an LLM call.

        Args:
            session_id: Platform session ID (injected by dispatch layer)
            prompt_text: User prompt text for heuristic title derivation
        """
        if not session_id:
            return {"success": False, "error": "session_id is required"}
        if session_manager is None:
            return {"success": False, "error": "session_manager is required"}
        try:
            title = await _bootstrap_session_title(session_manager, session_id, prompt_text)
            if not title:
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "title already set or prompt unusable",
                }
            return {"success": True, "title": title}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="sync_import",
        description="Import memories from .gobby/memories.jsonl into the database.",
    )
    async def sync_import() -> dict[str, Any]:
        """Import memories from filesystem JSONL into the hub database."""
        if not memory_sync_manager:
            return {"success": False, "error": "Memory sync manager not available"}
        try:
            result = await memory_sync_import(memory_sync_manager)
            if "error" in result:
                return {"success": False, "error": result["error"]}
            return {"success": True, "imported": result["imported"]["memories"]}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="sync_export",
        description="Export memories from the database to .gobby/memories.jsonl.",
    )
    async def sync_export() -> dict[str, Any]:
        """Export memories from the hub database to filesystem JSONL for Git persistence."""
        if not memory_sync_manager:
            return {"success": False, "error": "Memory sync manager not available"}
        try:
            project_id = get_current_project_id()
            result = await memory_sync_export(memory_sync_manager, project_id=project_id)
            if "error" in result:
                return {"success": False, "error": result["error"]}
            return {"success": True, "exported": result["exported"]["memories"]}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # NOTE: This tool is invoked via the `digest-on-response` DB rule (event=stop, mcp_call effect).
    # It is NOT called directly from Python code. Do not remove without also removing the DB rule.
    @registry.tool(
        name="build_turn_and_digest",
        description="Build a detailed turn record from the last agent response, append it to the session digest, and synthesize a title. Fired by digest-on-response rule on stop events.",
    )
    async def build_turn_and_digest_tool(
        session_id: str = "",
        prompt_text: str | None = None,
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
        """
        if not session_id:
            return {"success": False, "error": "session_id is required"}
        try:
            result = await _build_turn_and_digest(
                memory_manager=memory_manager,
                session_manager=session_manager,
                session_id=session_id,
                prompt_text=prompt_text,
                llm_service=llm_service,
                config=config,
            )
            if result is None:
                return {"success": True, "skipped": True, "reason": "disabled or no content"}
            if "error" in result:
                return {"success": False, "error": result["error"]}
            return {"success": True, **result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    register_memory_dream_tools(
        registry,
        memory_manager=memory_manager,
        llm_service=llm_service,
        config=config,
        get_project_id=get_current_project_id,
    )

    return registry
