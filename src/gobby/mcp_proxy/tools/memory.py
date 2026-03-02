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
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.memory.manager import MemoryManager
from gobby.workflows.memory_actions import (
    build_turn_and_digest as _build_turn_and_digest,
)
from gobby.workflows.memory_actions import (
    memory_extract_from_session,
)

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.llm.service import LLMService

logger = logging.getLogger(__name__)


# Helper to get current project context
def get_current_project_id() -> str | None:
    """Get the current project ID from context, or None if not in a project."""
    from gobby.utils.project_context import get_project_context

    ctx = get_project_context()
    if ctx and ctx.get("id"):
        return str(ctx["id"])
    return None


def create_memory_registry(
    memory_manager: MemoryManager,
    llm_service: LLMService | None = None,
    memory_sync_manager: Any | None = None,
    session_manager: Any | None = None,
    config: DaemonConfig | None = None,
) -> InternalToolRegistry:
    """
    Create a memory tool registry with all memory-related tools.

    Args:
        memory_manager: MemoryManager instance
        llm_service: LLM service for AI-powered extraction (optional)
        memory_sync_manager: MemorySyncManager for sync import/export (optional)
        session_manager: LocalSessionManager for session lookups (optional)
        config: DaemonConfig for digest provider/model selection (optional)

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

            memory = await memory_manager.create_memory(
                content=content,
                memory_type=memory_type,
                project_id=project_id,
                tags=tags,
                source_type="mcp_tool",
                source_session_id=resolved_session_id,
            )

            # Search for similar existing memories to surface potential duplicates
            similar_existing: list[dict[str, Any]] = []
            try:
                similar = await memory_manager.search_memories(
                    query=content,
                    project_id=project_id,
                    limit=4,  # fetch 4 since the new memory itself may appear
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
                    "Similarity search failed during memory creation "
                    "(project_id=%s, memory_id=%s): %s",
                    project_id,
                    memory.id,
                    e,
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
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Search memories based on query and filters.

        Args:
            query: Search query string
            limit: Maximum number of memories to return
            tags_all: Memory must have ALL of these tags
            tags_any: Memory must have at least ONE of these tags
            tags_none: Memory must have NONE of these tags
        """
        try:
            memories = await memory_manager.search_memories(
                query=query,
                project_id=get_current_project_id(),
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
                        "similarity": getattr(m, "similarity", None),  # Might be added by search
                    }
                    for m in memories
                ],
            }
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
            success = await memory_manager.delete_memory(memory_id)
            if success:
                return {"success": True}
            else:
                return {"success": False, "error": f"Memory {memory_id} not found"}
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
            memory = memory_manager.get_memory(memory_id)
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
        except ValueError as e:
            return {"success": False, "error": str(e)}
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
        except ValueError as e:
            return {"success": False, "error": str(e)}
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
            memory = await memory_manager.update_memory(
                memory_id=memory_id,
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
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="remember_with_image",
        description="Create a memory from an image. Uses LLM to describe the image and stores it with the description.",
    )
    async def remember_with_image(
        image_path: str,
        context: str | None = None,
        memory_type: str = "fact",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Create a memory from an image file.

        Uses the configured LLM provider to generate a description of the image,
        then stores the memory with the description as content and the image
        as a media attachment.

        Args:
            image_path: Path to the image file
            context: Optional context to guide the image description (e.g., "This is a screenshot of an error")
            memory_type: Type of memory (fact, preference, etc)
            tags: Optional list of tags
        """
        if not llm_service:
            return {
                "success": False,
                "error": "LLM service not configured. Image memories require an LLM provider.",
            }

        try:
            memory = await memory_manager.remember_with_image(
                image_path=image_path,
                context=context,
                memory_type=memory_type,
                project_id=get_current_project_id(),
                tags=tags,
                source_type="mcp_tool",
            )
            return {
                "success": True,
                "memory": {
                    "id": memory.id,
                    "content": memory.content,
                    "media_path": image_path,
                },
            }
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="remember_screenshot",
        description="Create a memory from raw screenshot bytes (base64 encoded). Saves to .gobby/resources/ and describes with LLM.",
    )
    async def remember_screenshot(
        screenshot_base64: str,
        context: str | None = None,
        memory_type: str = "observation",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Create a memory from raw screenshot bytes.

        Saves the screenshot to .gobby/resources/ with a timestamp-based filename,
        uses LLM to describe it, and stores the memory with the description.

        Args:
            screenshot_base64: Base64-encoded PNG screenshot bytes
            context: Optional context to guide the image description
            memory_type: Type of memory (default: "observation")
            tags: Optional list of tags
        """
        import base64

        if not llm_service:
            return {
                "success": False,
                "error": "LLM service not configured. Screenshot memories require an LLM provider.",
            }

        try:
            # Decode base64 to bytes
            screenshot_bytes = base64.b64decode(screenshot_base64)

            memory = await memory_manager.remember_screenshot(
                screenshot_bytes=screenshot_bytes,
                context=context,
                memory_type=memory_type,
                project_id=get_current_project_id(),
                tags=tags,
                source_type="mcp_tool",
            )
            return {
                "success": True,
                "memory": {
                    "id": memory.id,
                    "content": memory.content,
                },
            }
        except ValueError as e:
            return {"success": False, "error": str(e)}
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
        description="Search the Neo4j knowledge graph for entities matching a query.",
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

            results = await kg_service.search_graph(query, limit=limit)
            return {"success": True, "results": results}
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
        description="Extract entities and relationships from all memories into the Neo4j knowledge graph. Powers the 3D graph visualization.",
    )
    async def rebuild_knowledge_graph(
        project_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """
        Rebuild the knowledge graph from all existing memories.

        Extracts entities and relationships using LLM and stores them in Neo4j.
        This powers the 3D knowledge graph visualization.

        Args:
            project_id: Optional project ID to filter memories
            limit: Max memories to process (default 500)
        """
        try:
            kg = memory_manager.kg_service
            if not kg:
                return {
                    "success": False,
                    "error": "KnowledgeGraphService not initialized (requires Neo4j + LLM)",
                }
            memories = memory_manager.list_memories(project_id=project_id, limit=limit)
            extracted = 0
            errors = 0
            for i, memory in enumerate(memories):
                try:
                    await kg.add_to_graph(memory.content, memory_id=memory.id)
                    extracted += 1
                except Exception as e:
                    logger.warning(f"KG extraction failed for {memory.id}: {e}")
                    errors += 1
                if i % 10 == 9:
                    await asyncio.sleep(0)
            return {
                "success": True,
                "memories_processed": len(memories),
                "memories_extracted": extracted,
                "errors": errors,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ─── Sync & extraction tools (thin wrappers around workflow actions) ───

    @registry.tool(
        name="sync_import",
        description="Import memories from .gobby/memories.jsonl into the database.",
    )
    async def sync_import() -> dict[str, Any]:
        """Import memories from filesystem JSONL into SQLite."""
        if not memory_sync_manager:
            return {"success": False, "error": "Memory sync manager not available"}
        try:
            from gobby.workflows.memory_actions import memory_sync_import

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
        """Export memories from SQLite to filesystem JSONL for Git persistence."""
        if not memory_sync_manager:
            return {"success": False, "error": "Memory sync manager not available"}
        try:
            from gobby.workflows.memory_actions import memory_sync_export

            result = await memory_sync_export(memory_sync_manager)
            if "error" in result:
                return {"success": False, "error": result["error"]}
            return {"success": True, "exported": result["exported"]["memories"]}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="extract_from_session",
        description="Extract memories from a session transcript using LLM analysis. Safety net for capturing memories the agent didn't save.",
    )
    async def extract_from_session(
        session_id: str = "",
        max_memories: int = 5,
    ) -> dict[str, Any]:
        """
        Extract memories from a session transcript.

        Args:
            session_id: Session to extract from
            max_memories: Maximum memories to extract (default: 5)
        """
        if not session_id:
            return {"success": False, "error": "session_id is required"}
        try:
            result = await memory_extract_from_session(
                memory_manager=memory_manager,
                session_manager=session_manager,
                llm_service=llm_service,
                transcript_processor=None,
                session_id=session_id,
                max_memories=max_memories,
            )
            if result is None:
                return {"success": False, "error": "Memory manager disabled"}
            if "error" in result:
                return {"success": False, "error": result["error"]}
            return {"success": True, **result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # NOTE: This tool is invoked via the `digest-on-response` DB rule (event=stop, mcp_call effect).
    # It is NOT called directly from Python code. Do not remove without also removing the DB rule.
    @registry.tool(
        name="build_turn_and_digest",
        description="Build a detailed turn record from the last agent response, append to session digest, synthesize title, and extract memories. Fired by digest-on-response rule on stop events.",
    )
    async def build_turn_and_digest_tool(
        session_id: str = "",
        prompt_text: str | None = None,
    ) -> dict[str, Any]:
        """
        Build turn record and append to digest after agent response.

        Reads the last user/assistant exchange from the transcript,
        generates a structured turn record via LLM, appends it to the
        session's rolling digest, synthesizes a title, and extracts
        reusable memories.

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

    return registry
