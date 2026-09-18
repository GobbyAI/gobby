"""Automated memory surfacing tool registration."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.session_resolution import resolve_session_reference

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.memory.manager import MemoryManager
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

# Equal to ``surface_min_score`` in tests/memory/fixtures/ranking_cohort.json:
# the floor on undecayed similarity the graded ranking cohort settled on.
SURFACE_MIN_SCORE = 0.65
# Five lines is what an unasked-for index can spend of an agent's attention.
SURFACE_LIMIT = 5
# The whole text reaches the vector leg verbatim; the cap only keeps a runaway
# payload out of the embedding request.
MAX_QUERY_CHARS = 2000
SURFACE_CALLER = "memory.surface"
# Review lessons swamped generically worded queries in the graded cohort and
# already have their own injection path, so they never enter this index.
EXCLUDED_TAGS = ["review-lesson"]

SurfaceTrigger = Literal["turn", "spawn_agent", "task", "handoff"]


def _empty(trigger: str) -> dict[str, Any]:
    return {"trigger": trigger, "count": 0, "memories": []}


def _resolve_session(
    session_manager: SessionManager,
    session_id: str,
) -> tuple[str, Any] | None:
    session = session_manager.get(session_id)
    if session is not None:
        return str(session.id), session

    try:
        resolved_id = resolve_session_reference(session_manager.db, session_id)
    except ValueError:
        return None
    session = session_manager.get(resolved_id)
    return (resolved_id, session) if session is not None else None


def _serialize(memory: Any) -> dict[str, Any]:
    memory_type = getattr(memory, "memory_type", None)
    return {
        "id": memory.id,
        "type": str(memory_type) if memory_type is not None else None,
        "search_via": getattr(memory, "search_via", None),
        "updated_at": getattr(memory, "updated_at", None),
        "content": memory.content,
        "rationale": getattr(memory, "rationale", None),
    }


def register_memory_surface_tools(
    registry: InternalToolRegistry,
    memory_manager: Callable[[], MemoryManager],
    *,
    session_manager: SessionManager | None,
) -> None:
    """Register automated memory surfacing without granting memory writes."""

    @registry.tool(
        name="surface_memories",
        description=(
            "Search project/global memories related to a block of text and return the ranked "
            "hits as a compact index. Built for rule-driven surfacing at a turn, an agent "
            "spawn, a task claim, or a handoff; review lessons are excluded because they have "
            "their own injection path. Never writes memories, and returns an empty result "
            "rather than failing the caller."
        ),
    )
    async def surface_memories(
        text: str,
        trigger: SurfaceTrigger,
        session_id: str,
    ) -> dict[str, Any]:
        try:
            query = text.strip()[:MAX_QUERY_CHARS]
            if not query or session_manager is None:
                return _empty(trigger)

            resolved = await asyncio.to_thread(_resolve_session, session_manager, session_id)
            if resolved is None:
                return _empty(trigger)
            resolved_session_id, session = resolved
            project_id = getattr(session, "project_id", None)
            if not isinstance(project_id, str) or not project_id:
                return _empty(trigger)

            memories = await memory_manager().search_memories(
                # The search service embeds ``embed_text`` and hands ``query``
                # to the keyword leg; passing the same text keeps both verbatim.
                query=query,
                embed_text=query,
                project_id=project_id,
                limit=SURFACE_LIMIT,
                tags_none=list(EXCLUDED_TAGS),
                min_score=SURFACE_MIN_SCORE,
                session_id=resolved_session_id,
                # Joinable correlation id, minted per call: without it
                # insert_signal_event drops the event before the INSERT.
                recall_request_id=str(uuid4()),
                caller=SURFACE_CALLER,
            )
        except Exception:
            # Surfacing is unasked-for context; it never fails the caller's turn.
            logger.warning(
                "surface_memories failed for trigger %s, returning no memories",
                trigger,
                exc_info=True,
            )
            return _empty(trigger)

        serialized = [_serialize(memory) for memory in memories]
        return {"trigger": trigger, "count": len(serialized), "memories": serialized}
