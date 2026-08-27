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
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.memory_dream import register_memory_dream_tools
from gobby.mcp_proxy.tools.memory_review import register_memory_review_tools
from gobby.mcp_proxy.tools.memory_scope import (
    get_current_project_id,
    memory_owned_by_current_project,
)
from gobby.mcp_proxy.tools.memory_write import register_memory_write_tools
from gobby.memory.digest import (
    build_turn_and_digest as _build_turn_and_digest,
)
from gobby.memory.manager import MemoryManager
from gobby.memory.scoring import undecay
from gobby.storage.memories import MemoryType, validate_memory_type
from gobby.storage.projects import PERSONAL_PROJECT_ID

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.config.app import DaemonConfig
    from gobby.llm.service import LLMService
    from gobby.memory.dream.coordinator import MemoryDreamCoordinator

logger = logging.getLogger(__name__)

_SEARCH_CALLER = "mcp_proxy.memory.search_memories"


def _record_delivered_hits(
    manager: MemoryManager,
    *,
    session_id: str | None,
    recall_request_id: str,
    project_id: str,
    hits: list[dict[str, Any]],
) -> None:
    """Write one ``recall_injection_outcomes`` row per hit the agent received.

    The tool result is the delivery point of the search cohort (usefulness-label
    contract §5.1): a returned hit is ``injected`` at its list position; hits cut
    by ``limit`` or ``min_score`` never reach the agent and get no row. Fails
    open — the recorder is None while the signal hub is off and swallows its own
    write errors.
    """
    recorder = getattr(manager, "injection_outcome_recorder", None)
    if recorder is None or not session_id or not hits:
        return
    recorder(
        [
            {
                "session_id": session_id,
                "recall_request_id": recall_request_id,
                "memory_id": hit["id"],
                "project_id": project_id,
                "outcome": "injected",
                "injection_position": position,
                "caller": _SEARCH_CALLER,
            }
            for position, hit in enumerate(hits)
        ]
    )


def create_memory_registry(
    memory_manager_resolver: Callable[[], MemoryManager | None],
    llm_service_resolver: Callable[[], LLMService | None] | None = None,
    memory_backup_manager_resolver: Callable[[], Any | None] | None = None,
    session_manager: Any | None = None,
    startup_config: DaemonConfig | None = None,
    config_resolver: Callable[[], DaemonConfig | None] | None = None,
    dream_coordinator_resolver: Callable[[], MemoryDreamCoordinator | None] | None = None,
    task_manager: Any | None = None,
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
        task_manager: Task manager used to resolve and authorize closed-task reviews

    Returns:
        InternalToolRegistry with memory tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-memory",
        description=(
            "Memory management - create_memory, search_memories, review_task_memories, "
            "delete_memory, get_related_memories"
        ),
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

    register_memory_write_tools(registry, _memory_manager)
    register_memory_review_tools(
        registry,
        _memory_manager,
        task_manager=task_manager,
        session_manager=session_manager,
    )

    @registry.tool(
        name="search_memories",
        description=(
            "Hybrid search (semantic + keyword + graph) over memories with tag "
            "filtering. Scores are for the agent's judgment: raw cosine on the live "
            "corpus sits in a narrow band (p10 0.62 / p50 0.69 / p90 0.75), so read "
            "each hit's rationale and content rather than expecting a bimodal score. "
            "Near-duplicate hits are folded into the best-ranked one "
            "(collapsed_duplicates)."
        ),
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
            min_score: Optional floor (0.0-1.0) on the undecayed similarity —
                cosine times source boost with the age penalty removed, the same
                axis results are ranked on. 0.0 (default) returns the top ``limit``
                by rank; the scores are there for judgment, not admission.
            tags_all: Memory must have ALL of these tags
            tags_any: Memory must have at least ONE of these tags
            tags_none: Memory must have NONE of these tags

        Each hit carries ``similarity`` (age-decayed), ``raw_semantic_score``,
        ``undecayed_similarity``, ``rationale``, ``updated_at``,
        ``graph_confidence``, ``source_task_id``, ``created_by_agent``, and
        ``collapsed_duplicates`` (ids of near-identical lower-ranked hits folded
        into this one). ``diagnostics`` always reports ``candidates_considered``
        and the undecayed ``score_range``.
        """
        try:
            from uuid import uuid4

            from gobby.utils.session_context import get_current_session_id

            effective_min_score = min_score if min_score > 0 else 0.0
            # Joinable correlation id (contract §2): threads the signal event,
            # the returned payload, and any downstream injection outcome.
            recall_request_id = str(uuid4())
            current_session_id = get_current_session_id()
            current_project_id = get_current_project_id() or PERSONAL_PROJECT_ID
            canonical_memory_type = (
                validate_memory_type(memory_type) if memory_type is not None else None
            )

            # The floor travels to the service so its backfill loop chases the
            # same undecayed axis this tool reports (#21010).
            manager = _memory_manager()
            candidates = await manager.search_memories(
                query=query,
                project_id=current_project_id,
                limit=limit,
                min_score=effective_min_score if effective_min_score > 0 else None,
                memory_type=canonical_memory_type,
                tags_all=tags_all,
                tags_any=tags_any,
                tags_none=tags_none,
                session_id=current_session_id,
                recall_request_id=recall_request_id,
                caller=_SEARCH_CALLER,
            )

            hits: list[dict[str, Any]] = []
            undecayed_scores: list[float] = []
            for m in candidates:
                similarity = getattr(m, "similarity", None)
                undecayed_similarity = (
                    undecay(similarity, getattr(m, "temporal_decay_factor", None))
                    if isinstance(similarity, int | float) and not isinstance(similarity, bool)
                    else None
                )
                if undecayed_similarity is not None:
                    undecayed_scores.append(undecayed_similarity)
                    if effective_min_score > 0 and undecayed_similarity < effective_min_score:
                        continue
                if len(hits) < limit:
                    hits.append(
                        {
                            "id": m.id,
                            "content": m.content,
                            "rationale": getattr(m, "rationale", None),
                            "type": m.memory_type,
                            "created_at": m.created_at,
                            "updated_at": getattr(m, "updated_at", None),
                            "tags": m.tags,
                            "project_id": m.project_id,
                            "is_global": m.is_global,
                            "source_task_id": getattr(m, "source_task_id", None),
                            "created_by_agent": getattr(m, "created_by_agent", None),
                            "similarity": similarity,
                            "undecayed_similarity": undecayed_similarity,
                            "search_via": getattr(m, "search_via", None),
                            "ranking_score": getattr(m, "ranking_score", None),
                            "raw_semantic_score": getattr(m, "raw_semantic_score", None),
                            "temporal_decay_factor": getattr(m, "temporal_decay_factor", None),
                            "graph_confidence": getattr(m, "graph_confidence", None),
                            "ranking_mode": getattr(m, "ranking_mode", None),
                            "collapsed_duplicates": getattr(m, "collapsed_duplicates", None),
                        }
                    )

            _record_delivered_hits(
                manager,
                session_id=current_session_id,
                recall_request_id=recall_request_id,
                project_id=current_project_id,
                hits=hits,
            )
            return {
                "success": True,
                "memories": hits,
                "recall_request_id": recall_request_id,
                "project_id": current_project_id,
                "diagnostics": {
                    "candidates_considered": len(candidates),
                    "returned": len(hits),
                    "threshold": effective_min_score,
                    "threshold_axis": "undecayed_similarity",
                    "score_range": (
                        [round(min(undecayed_scores), 4), round(max(undecayed_scores), 4)]
                        if undecayed_scores
                        else None
                    ),
                },
            }
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
            if not memory_owned_by_current_project(existing):
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
            if not memory_owned_by_current_project(existing):
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
            if not memory_owned_by_current_project(existing):
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
                        "rationale": getattr(m, "rationale", None),
                        "type": m.memory_type,
                        "created_at": m.created_at,
                        "updated_at": getattr(m, "updated_at", None),
                        "tags": m.tags,
                        "project_id": m.project_id,
                        "is_global": m.is_global,
                        "source_task_id": getattr(m, "source_task_id", None),
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
                        "rationale": getattr(memory, "rationale", None),
                        "type": memory.memory_type,
                        "created_at": memory.created_at,
                        "updated_at": memory.updated_at,
                        "project_id": memory.project_id,
                        "is_global": memory.is_global,
                        "source_type": memory.source_type,
                        "source_task_id": getattr(memory, "source_task_id", None),
                        "created_by_agent": getattr(memory, "created_by_agent", None),
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
                        "similarity": getattr(m, "similarity", None),
                    }
                    for m in memories
                ],
                "count": len(memories),
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
        description=(
            "Restore memories from ~/.gobby/backups/<project-uuid>/memories.jsonl "
            "when backup timestamps win."
        ),
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
        description=(
            "Write current live memories to ~/.gobby/backups/<project-uuid>/memories.jsonl."
        ),
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

    register_memory_dream_tools(
        registry,
        coordinator_resolver=dream_coordinator_resolver or (lambda: None),
        get_project_id=get_current_project_id,
    )

    return registry
