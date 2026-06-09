"""Stale candidate discovery for memory dream."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from gobby.memory.dream.models import DreamCandidate

logger = logging.getLogger(__name__)


class MemoryManagerProtocol(Protocol):
    async def alist_memories(self, *, limit: int | None, offset: int) -> list[Any]: ...


class DreamConfigProtocol(Protocol):
    candidate_page_timeout_seconds: float
    max_scan_rows: int
    scan_limit: int
    stale_age_days: int
    include_global_memories: bool


async def discover_stale_candidates(
    memory_manager: MemoryManagerProtocol,
    dream_config: DreamConfigProtocol,
    *,
    project_id: str | None = None,
    memory_type: str | None = None,
    now: datetime | None = None,
) -> list[DreamCandidate]:
    """Find old memories by age and scope.

    Access statistics are preserved as classifier context, but they do not
    decide eligibility.
    """
    now = now or datetime.now(UTC)
    candidates: list[DreamCandidate] = []
    offset = 0
    scanned = 0
    max_scan_rows = _positive_int_value(dream_config.max_scan_rows, "max_scan_rows", 5000)
    page_size = min(500, max_scan_rows)
    scan_limit = _positive_int_value(dream_config.scan_limit, "scan_limit", 500)
    stale_age_days = _positive_int_value(dream_config.stale_age_days, "stale_age_days", 30)
    page_timeout = _positive_float_value(
        getattr(dream_config, "candidate_page_timeout_seconds", 10.0),
        "candidate_page_timeout_seconds",
        10.0,
    )
    include_global = _bool_value(
        dream_config.include_global_memories,
        "include_global_memories",
        True,
    )

    while scanned < max_scan_rows and len(candidates) < scan_limit:
        page_limit = min(page_size, max_scan_rows - scanned)
        try:
            page = await asyncio.wait_for(
                memory_manager.alist_memories(limit=page_limit, offset=offset),
                timeout=page_timeout,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                f"Timed out after {page_timeout:g}s listing memory dream candidates "
                f"at offset {offset}"
            ) from exc
        if not page:
            break

        scanned += len(page)
        offset += len(page)
        for memory in page:
            if memory_type and getattr(memory, "memory_type", None) != memory_type:
                continue
            if not _in_scope(getattr(memory, "project_id", None), project_id, include_global):
                continue

            age_days = _age_days(memory, now)
            if age_days is None or age_days < stale_age_days:
                continue

            reasons = [f"updated_at older than {stale_age_days} days"]
            if getattr(memory, "project_id", None) is None:
                reasons.append("global memory")
            candidates.append(
                DreamCandidate(
                    id=str(memory.id),
                    content=str(memory.content),
                    memory_type=str(memory.memory_type),
                    project_id=getattr(memory, "project_id", None),
                    source_type=getattr(memory, "source_type", None),
                    source_session_id=getattr(memory, "source_session_id", None),
                    tags=list(getattr(memory, "tags", None) or []),
                    age_days=age_days,
                    access_count=_int_attr(memory, "access_count"),
                    created_at=str(getattr(memory, "created_at", "")),
                    updated_at=str(getattr(memory, "updated_at", "")),
                    last_accessed_at=getattr(memory, "last_accessed_at", None),
                    reasons=reasons,
                )
            )
            if len(candidates) >= scan_limit:
                break

        if len(page) < page_limit:
            break

    return sorted(candidates, key=_candidate_sort_key)


def _in_scope(memory_project_id: str | None, project_id: str | None, include_global: bool) -> bool:
    if project_id is None:
        return True
    if memory_project_id == project_id:
        return True
    return include_global and memory_project_id is None


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


def _candidate_sort_key(item: DreamCandidate) -> tuple[float, datetime]:
    created_at = _parse_datetime(item.created_at)
    if created_at is None:
        created_at = datetime.max.replace(tzinfo=UTC)
    return (-item.age_days, created_at)


def _positive_int_value(value: Any, attr: str, default: int) -> int:
    if isinstance(value, bool):
        logger.warning("Invalid memory dream %s=%r; using default %s", attr, value, default)
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid memory dream %s=%r; using default %s", attr, value, default)
        return default
    if parsed < 1:
        logger.warning("Invalid memory dream %s=%r; using default %s", attr, value, default)
        return default
    return parsed


def _bool_value(value: Any, attr: str, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    logger.warning("Invalid memory dream %s=%r; using default %s", attr, value, default)
    return default


def _positive_float_value(value: Any, attr: str, default: float) -> float:
    if isinstance(value, bool):
        logger.warning("Invalid memory dream %s=%r; using default %s", attr, value, default)
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        logger.warning("Invalid memory dream %s=%r; using default %s", attr, value, default)
        return default
    if parsed <= 0.0:
        logger.warning("Invalid memory dream %s=%r; using default %s", attr, value, default)
        return default
    return parsed


def _int_attr(obj: Any, attr: str) -> int:
    value = getattr(obj, attr, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
