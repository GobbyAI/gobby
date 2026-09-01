"""Write-path memory tools: create, update, delete, and restore.

Split out of `memory.py`, which sat one line under the production ceiling. This
module owns the write surface and its vocabulary — rationale normalization,
create-time provenance derivation, and the auto-supersede threshold. Read and
maintenance tools stay in `memory.py`; both import scope resolution from
`memory_scope.py`, so the dependency graph stays acyclic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.memory_scope import (
    get_current_project_id,
    memory_owned_by_current_project,
    resolve_current_memory_id,
)
from gobby.memory.embedding_text import memory_embedding_text
from gobby.memory.scoring import undecay
from gobby.storage.memories import MemoryType, validate_memory_type
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.sync.memories import is_ephemeral_implementation_note
from gobby.utils.session_context import get_current_session_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.memory.manager import MemoryManager

logger = logging.getLogger(__name__)

# Raw cosine at or above which create_memory supersedes the existing memory
# instead of leaving a permanent near-duplicate (dream no longer merges). Read on
# ``raw_semantic_score`` -- the bare cosine, before source boost and age decay --
# so an old duplicate is still recognised as one (#21010).
AUTO_SUPERSEDE_SIMILARITY = 0.9
# How many neighbours the write-time probe reports back to the writer.
SIMILAR_EXISTING_LIMIT = 5
RATIONALE_REQUIRED_ERROR = (
    "rationale_required: one or two sentences on why this memory should be "
    "re-served to future sessions (max 500 chars)"
)
_RATIONALE_MAX_LEN = 500


def normalize_memory_rationale(rationale: str | None) -> str | None:
    stripped = "" if rationale is None else rationale.strip()
    return stripped if stripped and len(stripped) <= _RATIONALE_MAX_LEN else None


def derive_memory_create_provenance(
    db: Any,
    *,
    project_id: str,
    resolved_session_id: str | None,
    source_task_id: str | None = None,
    created_by_agent: str | None = None,
) -> tuple[str | None, str | None]:
    """Derive task/agent provenance. Lookup failures degrade to None."""
    from gobby.storage.tasks import TaskNotFoundError

    task_id: str | None = None
    try:
        if source_task_id:
            from gobby.storage.tasks._id import resolve_task_reference

            task_id = resolve_task_reference(db, source_task_id, project_id)
        elif resolved_session_id:
            from gobby.storage.tasks import LocalTaskManager

            claimed = LocalTaskManager(db).list_tasks(
                claimed_by_session_id=resolved_session_id,
                closed=False,
                sort_by="updated_at",
                sort_order="desc",
            )
            task_id = str(claimed[0].id) if claimed else None
    except (ValueError, LookupError, TaskNotFoundError):
        logger.debug("Could not derive source_task_id", exc_info=True)
        task_id = None
    if created_by_agent or not resolved_session_id:
        return task_id, created_by_agent
    try:
        from gobby.storage.agents import LocalAgentRunManager

        run = LocalAgentRunManager(db).get_by_session(resolved_session_id)
        if run is not None and run.agent_name:
            return task_id, str(run.agent_name)
        from gobby.storage.sessions import SessionManager

        session = SessionManager(db).get(resolved_session_id)
        source = getattr(session, "source", None) if session is not None else None
        return task_id, str(source) if source else None
    except (ValueError, LookupError, TaskNotFoundError):
        logger.debug("Could not derive created_by_agent", exc_info=True)
        return task_id, None


def register_memory_write_tools(
    registry: InternalToolRegistry,
    memory_manager: Callable[[], MemoryManager],
) -> None:
    """Register the memory write tools on an existing registry.

    Args:
        registry: the memory registry being assembled
        memory_manager: resolver returning the live MemoryManager, raising when
            memory services are unavailable in the current runtime epoch
    """

    @registry.tool(
        name="create_memory",
        description=(
            "Create a new memory. Bugs, incidents, and incorrect runtime "
            "behavior belong on gobby-tasks.create_task with claim=true, not "
            "here. rationale is mandatory: one or two sentences on why a "
            "future, unrelated session should be served this memory (max 500 "
            "chars). source_task_id and created_by_agent are derived unless "
            "overridden. Returns similar existing memories to help detect "
            "duplicates."
        ),
    )
    async def create_memory(
        content: str,
        rationale: str | None = None,
        memory_type: MemoryType | Literal["implementation_note"] = MemoryType.FACT,
        tags: list[str] | None = None,
        supersedes: list[str] | None = None,
        session_id: str | None = None,
        source_task_id: str | None = None,
        created_by_agent: str | None = None,
        is_global: bool = False,
    ) -> dict[str, Any]:
        """
        Create a new memory.

        Bugs, incidents, and incorrect runtime behavior belong on
        ``gobby-tasks.create_task`` with ``claim=true``, not here.

        Args:
            content: The memory content to store
            rationale: Durable-value claim — why a future, unrelated session
                should see this. Run logs, status snapshots, and one-time
                results do not qualify. Max 500 characters.
            memory_type: Type of memory (fact, preference, etc)
            tags: Optional list of tags
            supersedes: Up to 20 memory UUIDs to atomically soft-hide while recording
                ``supersedes:<id>`` provenance on the resulting memory. Use this for
                durable decisions, removals, and replacements. Near-duplicates
                (raw cosine >= 0.9) are superseded automatically and reported in
                ``auto_superseded``; ``similar_existing`` lists the five nearest
                neighbours with their undecayed similarity so the writer can judge
                overlap the automatic threshold missed.
            session_id: Session ID that created this memory (accepts #N, N, UUID, or
                prefix); defaults to the calling session's context
            source_task_id: Optional task override (#N or UUID); derived from the
                session's open claim when omitted
            created_by_agent: Optional agent-name override; derived from the
                agent run or CLI source when omitted
        """
        try:
            from gobby.storage.memories_crud import normalize_supersedes

            normalized_rationale = normalize_memory_rationale(rationale)
            if normalized_rationale is None:
                return {"success": False, "error": RATIONALE_REQUIRED_ERROR}

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

            # The proxy injects the caller's session only into tools whose schema
            # requires ``session_id``; this one is optional, so fall back to the
            # seeded session context (already a UUID) when no explicit ref is given.
            resolved_session_id: str | None = None
            if session_id:
                try:
                    from gobby.storage.session_resolution import resolve_session_reference

                    resolved_session_id = await asyncio.to_thread(
                        resolve_session_reference,
                        memory_manager().db,
                        session_id,
                        project_id,
                    )
                except ValueError as e:
                    logger.warning("Could not resolve session_id '%s': %s", session_id, e)
            else:
                resolved_session_id = get_current_session_id()

            similar_existing: list[dict[str, Any]] = []
            auto_superseded: list[dict[str, Any]] = []
            try:
                # The probe embeds what the stored row will embed -- content plus
                # rationale -- so it sees the corpus the way search will (#21010).
                similar = await memory_manager().search_memories(
                    query=content,
                    project_id=project_id,
                    limit=SIMILAR_EXISTING_LIMIT,
                    embed_text=memory_embedding_text(content, normalized_rationale),
                    caller="mcp_proxy.memory.create_memory.similar_existing",
                )
                for m in similar:
                    similarity = getattr(m, "similarity", None)
                    raw_score = getattr(m, "raw_semantic_score", None)
                    undecayed = (
                        undecay(similarity, getattr(m, "temporal_decay_factor", None))
                        if isinstance(similarity, int | float) and not isinstance(similarity, bool)
                        else None
                    )
                    similar_existing.append(
                        {
                            "id": m.id,
                            "content": m.content,
                            "rationale": getattr(m, "rationale", None),
                            "similarity": undecayed,
                            "raw_semantic_score": raw_score,
                        }
                    )
                    if (
                        isinstance(raw_score, int | float)
                        and not isinstance(raw_score, bool)
                        and float(raw_score) >= AUTO_SUPERSEDE_SIMILARITY
                        and m.id not in supersedes_ids
                    ):
                        auto_superseded.append({"id": m.id, "similarity": float(raw_score)})
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

            derived_task_id, derived_agent = await asyncio.to_thread(
                derive_memory_create_provenance,
                memory_manager().db,
                project_id=project_id,
                resolved_session_id=resolved_session_id,
                source_task_id=source_task_id,
                created_by_agent=created_by_agent,
            )
            memory = await memory_manager().create_memory(
                content=content,
                memory_type=canonical_memory_type,
                project_id=project_id,
                tags=tags,
                supersedes=supersedes_ids,
                source_type="agent",
                source_session_id=resolved_session_id,
                is_global=is_global,
                rationale=normalized_rationale,
                source_task_id=derived_task_id,
                created_by_agent=derived_agent,
            )

            result: dict[str, Any] = {
                "success": True,
                "memory": {
                    "id": memory.id,
                    "project_id": memory.project_id,
                    "is_global": memory.is_global,
                    "rationale": normalized_rationale,
                    "source_task_id": derived_task_id,
                    "created_by_agent": derived_agent,
                },
                "similar_existing": similar_existing,
            }
            if auto_superseded:
                result["auto_superseded"] = auto_superseded
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    create_meta = registry.get_tool_metadata("create_memory")
    if create_meta is not None:
        required = create_meta.input_schema.setdefault("required", [])
        if "rationale" not in required:
            required.append("rationale")

    @registry.tool(
        name="update_memory",
        description="Update an existing memory's content or tags.",
    )
    async def update_memory(
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        rationale: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> dict[str, Any]:
        """
        Update an existing memory.

        Args:
            memory_id: The ID of the memory to update
            content: New content (optional). A content change requires a fresh
                ``rationale`` -- the durable-value claim is re-argued with the
                body it describes.
            tags: New list of tags (optional)
            rationale: New durable-value claim (max 500 characters); required when
                ``content`` is given, optional otherwise
            memory_type: New type (fact, preference, pattern, context)
        """
        try:
            normalized_rationale = normalize_memory_rationale(rationale)
            if content is not None and normalized_rationale is None:
                return {"success": False, "error": RATIONALE_REQUIRED_ERROR}
            if rationale is not None and normalized_rationale is None:
                return {"success": False, "error": RATIONALE_REQUIRED_ERROR}
            canonical_memory_type = (
                validate_memory_type(memory_type) if memory_type is not None else None
            )
            resolved_id = resolve_current_memory_id(memory_manager(), memory_id)
            if resolved_id is None:
                return {"success": False, "error": f"Memory {memory_id} not found"}
            memory = await memory_manager().update_memory_scoped(
                memory_id=resolved_id,
                project_id=get_current_project_id() or PERSONAL_PROJECT_ID,
                content=content,
                tags=tags,
                memory_type=canonical_memory_type,
                rationale=normalized_rationale,
            )
            return {
                "success": True,
                "memory": {
                    "id": memory.id,
                    "updated_at": memory.updated_at,
                    "project_id": memory.project_id,
                    "is_global": memory.is_global,
                    "type": memory.memory_type,
                    "rationale": getattr(memory, "rationale", None),
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="delete_memory",
        description="Delete a memory by ID.",
    )
    async def delete_memory(memory_id: str) -> dict[str, Any]:
        """
        Hard-delete a memory by ID. Unrecoverable.

        When a replacement exists, prefer ``create_memory(..., supersedes=[id])``:
        superseding soft-hides the old row with provenance and ``restore_memory``
        can bring it back. Reserve deletion for rows that are wrong or worthless
        with nothing to replace them.

        Args:
            memory_id: The ID of the memory to delete
        """
        try:
            resolved_id = resolve_current_memory_id(memory_manager(), memory_id)
            if resolved_id is None:
                return {"success": False, "error": f"Memory {memory_id} not found"}
            success = await memory_manager().delete_memory_scoped(
                resolved_id,
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
            resolved_id = resolve_current_memory_id(memory_manager(), memory_id)
            if resolved_id is None:
                return {"success": False, "error": f"Memory {memory_id} not found"}
            memory = memory_manager().get_memory(resolved_id, visibility="all")
            if memory is None or not memory_owned_by_current_project(memory):
                return {"success": False, "error": f"Memory {memory_id} not found"}
            await asyncio.to_thread(memory_manager().restore_memory, resolved_id)
            await memory_manager().restore_memory_indices(
                memory.id,
                memory.content,
                memory.project_id,
                memory.is_global,
                getattr(memory.memory_type, "value", memory.memory_type),
            )
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
