"""Active-sweep candidate discovery for memory dream.

Discovery is re-centered on *current truth*, not age. One page at a time, the
storage cooldown query returns active memories that have never been dreamed or
whose last sweep predates the cooldown cutoff. Each returned row is stamped by
the apply step, so it drops out of the next page and the loop drains to zero.
Age is retained only as classifier context for the planner, never as a gate.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Protocol

from gobby.memory.dream.models import DreamCandidate
from gobby.storage.memories_scope import MemoryScope
from gobby.utils.datetime import parse_stored_datetime, require_stored_datetime, utc_now

logger = logging.getLogger(__name__)


class SweepCandidateSource(Protocol):
    def list_dream_candidates(
        self,
        *,
        limit: int,
        redream_cutoff: str,
        scope: MemoryScope,
        memory_type: str | None = None,
    ) -> list[Any]: ...

    def get_memories(self, memory_ids: list[str], scope: MemoryScope) -> list[Any]: ...


async def list_sweep_candidates(
    memory_manager: SweepCandidateSource,
    *,
    limit: int,
    redream_cutoff: str,
    scope: MemoryScope,
    memory_type: str | None = None,
    candidate_ids: list[str] | None = None,
    now: datetime | None = None,
) -> list[DreamCandidate]:
    """Fetch one page of active sweep candidates and adapt them for planning.

    Normal sweeps apply scope and cooldown through ``list_dream_candidates``.
    Snapshot pages hydrate requested IDs in order and skip rows no longer active
    or visible. This helper adapts either source into ``DreamCandidate`` context.
    """
    now = now or utc_now()
    if candidate_ids is None:
        rows = await asyncio.to_thread(
            memory_manager.list_dream_candidates,
            limit=limit,
            redream_cutoff=redream_cutoff,
            scope=scope,
            memory_type=memory_type,
        )
    else:
        rows = await asyncio.to_thread(memory_manager.get_memories, candidate_ids, scope)
        found_ids = {str(row.id) for row in rows}
        for memory_id in candidate_ids:
            if memory_id not in found_ids:
                logger.info("Skipping missing dream snapshot candidate %s", memory_id)
    return [memory_to_candidate(row, now) for row in rows]


def memory_to_candidate(memory: Any, now: datetime) -> DreamCandidate:
    """Adapt a stored memory row into planner-facing dream candidate context."""
    age_days = _age_days(memory, now)
    reasons: list[str] = []
    if getattr(memory, "last_dreamed_at", None):
        reasons.append("re-dream cooldown elapsed")
    else:
        reasons.append("never dreamed")
    if bool(getattr(memory, "is_global", False)):
        reasons.append("global memory")
    return DreamCandidate(
        id=str(memory.id),
        content=str(memory.content),
        memory_type=str(memory.memory_type),
        project_id=str(memory.project_id),
        is_global=bool(memory.is_global),
        source_type=getattr(memory, "source_type", None),
        source_session_id=getattr(memory, "source_session_id", None),
        rationale=getattr(memory, "rationale", None),
        source_task_id=getattr(memory, "source_task_id", None),
        created_by_agent=getattr(memory, "created_by_agent", None),
        tags=list(getattr(memory, "tags", None) or []),
        age_days=age_days if age_days is not None else 0.0,
        access_count=_int_attr(memory, "access_count"),
        created_at=require_stored_datetime(getattr(memory, "created_at", None), "created_at"),
        updated_at=require_stored_datetime(getattr(memory, "updated_at", None), "updated_at"),
        last_accessed_at=getattr(memory, "last_accessed_at", None),
        dream_due_version=_int_attr(memory, "dream_due_version"),
        reasons=reasons,
    )


def _age_days(memory: Any, now: datetime) -> float | None:
    """Return age in days from updated/created timestamp, or None when unavailable."""
    updated = _parse_datetime(getattr(memory, "updated_at", None))
    created = _parse_datetime(getattr(memory, "created_at", None))
    timestamp = updated or created
    if timestamp is None:
        return None
    return max(0.0, (now - timestamp).total_seconds() / 86400)


def _parse_datetime(value: Any) -> datetime | None:
    return parse_stored_datetime(value)


def _int_attr(obj: Any, attr: str) -> int:
    value = getattr(obj, attr, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
