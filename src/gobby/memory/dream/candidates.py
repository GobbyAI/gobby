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
from datetime import UTC, datetime
from typing import Any, Protocol

from gobby.memory.dream.models import DreamCandidate

logger = logging.getLogger(__name__)


class SweepCandidateSource(Protocol):
    def list_dream_candidates(
        self,
        *,
        limit: int,
        redream_cutoff: str,
        project_id: str | None = None,
        memory_type: str | None = None,
        include_global: bool = True,
        global_only: bool = False,
    ) -> list[Any]: ...


async def list_sweep_candidates(
    memory_manager: SweepCandidateSource,
    *,
    limit: int,
    redream_cutoff: str,
    project_id: str | None = None,
    memory_type: str | None = None,
    include_global: bool = True,
    global_only: bool = False,
    now: datetime | None = None,
) -> list[DreamCandidate]:
    """Fetch one page of active sweep candidates and adapt them for planning.

    Project/global and memory-type scope plus the cooldown cutoff are applied in
    SQL by ``list_dream_candidates``; this helper only adapts the rows into
    ``DreamCandidate`` prompt context.
    """
    now = now or datetime.now(UTC)
    rows = await asyncio.to_thread(
        memory_manager.list_dream_candidates,
        limit=limit,
        redream_cutoff=redream_cutoff,
        project_id=project_id,
        memory_type=memory_type,
        include_global=include_global,
        global_only=global_only,
    )
    return [memory_to_candidate(row, now) for row in rows]


def memory_to_candidate(memory: Any, now: datetime) -> DreamCandidate:
    """Adapt a stored memory row into planner-facing dream candidate context."""
    age_days = _age_days(memory, now)
    reasons: list[str] = []
    if getattr(memory, "last_dreamed_at", None):
        reasons.append("re-dream cooldown elapsed")
    else:
        reasons.append("never dreamed")
    if getattr(memory, "project_id", None) is None:
        reasons.append("global memory")
    return DreamCandidate(
        id=str(memory.id),
        content=str(memory.content),
        memory_type=str(memory.memory_type),
        project_id=getattr(memory, "project_id", None),
        source_type=getattr(memory, "source_type", None),
        source_session_id=getattr(memory, "source_session_id", None),
        tags=list(getattr(memory, "tags", None) or []),
        age_days=age_days if age_days is not None else 0.0,
        access_count=_int_attr(memory, "access_count"),
        created_at=str(getattr(memory, "created_at", "")),
        updated_at=str(getattr(memory, "updated_at", "")),
        last_accessed_at=getattr(memory, "last_accessed_at", None),
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
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _int_attr(obj: Any, attr: str) -> int:
    value = getattr(obj, attr, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
