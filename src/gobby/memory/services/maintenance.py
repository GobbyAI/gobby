"""Memory maintenance functions: stats, export, and finder helpers.

Extracted from manager.py as part of Strangler Fig decomposition (Wave 2).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from gobby.memory.embedding_text import memory_embedding_text
from gobby.memory.vectorstore import memory_scope_filter
from gobby.storage.memories import Memory
from gobby.storage.memories_scope import ALL_MEMORIES, MemoryScope

if TYPE_CHECKING:
    from gobby.memory.vectorstore import VectorStore
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.memories import LocalMemoryManager

logger = logging.getLogger(__name__)


def _maintenance_scope(project_id: str | None) -> MemoryScope:
    if project_id is None:
        return ALL_MEMORIES
    return MemoryScope.project_visible(project_id)


async def get_stats(
    storage: LocalMemoryManager,
    db: HubDatabase,
    project_id: str | None = None,
    vector_store: VectorStore | None = None,
) -> dict[str, Any]:
    """Get statistics about stored memories.

    Args:
        storage: Local memory storage manager.
        db: Database connection.
        project_id: Optional project to filter stats by.
        vector_store: Optional VectorStore for vector count stats.

    Returns:
        Dictionary with memory statistics.
    """
    memories = await asyncio.to_thread(
        storage.list_memories,
        scope=_maintenance_scope(project_id),
        limit=10000,
    )

    if not memories:
        return {
            "total_count": 0,
            "by_type": {},
            "recent_count": 0,
            "project_id": project_id,
        }

    by_type: dict[str, int] = {}
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    recent_count = 0

    for m in memories:
        by_type[m.memory_type] = by_type.get(m.memory_type, 0) + 1
        if m.created_at > cutoff:
            recent_count += 1

    stats: dict[str, Any] = {
        "total_count": len(memories),
        "by_type": by_type,
        "recent_count": recent_count,
        "project_id": project_id,
    }

    if vector_store is not None:
        try:
            stats["vector_count"] = await vector_store.count()
        except Exception:
            logger.warning(
                "Failed to retrieve memory vector count",
                extra={"project_id": project_id},
                exc_info=True,
            )
            stats["vector_count"] = -1

    return stats


def export_markdown(
    storage: LocalMemoryManager,
    project_id: str | None = None,
    include_metadata: bool = True,
    include_stats: bool = True,
) -> str:
    """Export memories as a formatted markdown document.

    Args:
        storage: Local memory storage manager.
        project_id: Filter by project ID (None for all memories).
        include_metadata: Include memory metadata (type, tags).
        include_stats: Include summary statistics at the top.

    Returns:
        Formatted markdown string with all memories.
    """
    memories = storage.list_memories(scope=_maintenance_scope(project_id), limit=10000)

    lines: list[str] = []

    lines.append("# Memory Export")
    lines.append("")

    if include_stats:
        now = datetime.now(UTC)
        lines.append(f"**Exported:** {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        lines.append(f"**Total memories:** {len(memories)}")
        if project_id:
            lines.append(f"**Project:** {project_id}")

        if memories:
            by_type: dict[str, int] = {}
            for m in memories:
                by_type[m.memory_type] = by_type.get(m.memory_type, 0) + 1
            type_str = ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))
            lines.append(f"**By type:** {type_str}")

        lines.append("")
        lines.append("---")
        lines.append("")

    for memory in memories:
        short_id = memory.id[:8] if len(memory.id) > 8 else memory.id
        lines.append(f"## Memory: {short_id}")
        lines.append("")

        lines.append(memory.content)
        lines.append("")

        if include_metadata:
            _append_metadata(lines, memory)

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _append_metadata(lines: list[str], memory: Memory) -> None:
    """Append memory metadata lines to the export."""
    lines.append(f"- **Type:** {memory.memory_type}")

    if memory.tags:
        tags_str = ", ".join(memory.tags)
        lines.append(f"- **Tags:** {tags_str}")

    if memory.source_type:
        lines.append(f"- **Source:** {memory.source_type}")

    created_str = memory.created_at.strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"- **Created:** {created_str}")

    if memory.access_count > 0:
        lines.append(f"- **Accessed:** {memory.access_count} times")

    lines.append("")


# ---------------------------------------------------------------------------
# Finder helpers reused by memory dream.
# ---------------------------------------------------------------------------

# Patterns that indicate a memory is just describing code structure.
# These memories can be re-derived by reading the codebase.
_CODE_DERIVABLE_PATTERNS: list[re.Pattern[str]] = [
    # "File X contains ...", "The file X has ..."
    re.compile(
        r"^(?:the\s+)?file\s+[`'\"]?[\w./-]+[`'\"]?\s+(?:contains?|has|defines?|exports?|includes?)",
        re.IGNORECASE,
    ),
    # "Function/method/class X is defined in Y"
    re.compile(
        r"^(?:the\s+)?(?:function|method|class|module|variable|constant)\s+[`'\"]?\w+[`'\"]?\s+"
        r"(?:is\s+)?(?:defined|located|found|declared)\s+in",
        re.IGNORECASE,
    ),
    # "The directory X contains ..."
    re.compile(
        r"^(?:the\s+)?directory\s+[`'\"]?[\w./-]+[`'\"]?\s+(?:contains?|has|holds)",
        re.IGNORECASE,
    ),
    # "X is imported from Y" / "import X from Y"
    re.compile(
        r"^(?:the\s+)?(?:import|from)\s+[`'\"]?[\w./-]+[`'\"]?",
        re.IGNORECASE,
    ),
    # Bare file path (just a path, nothing else)
    re.compile(r"^[`'\"]?[\w./-]+\.(?:py|ts|tsx|js|jsx|yaml|yml|json|toml|md|rs|go)[`'\"]?\s*$"),
]

# Maximum content length for code-derivable heuristic — longer memories
# are more likely to contain substantive context beyond code structure.
_CODE_DERIVABLE_MAX_LEN = 200


async def find_duplicate_memories(
    storage: LocalMemoryManager,
    vector_store: VectorStore,
    embed_fn: Callable[..., Any],
    project_id: str | None = None,
    similarity_threshold: float = 0.95,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Find near-duplicate memory pairs using vector similarity.

    For each pair, determines which to keep (higher access_count, then more
    recent updated_at) and which to delete.

    Args:
        storage: Local memory storage.
        vector_store: VectorStore for similarity search.
        embed_fn: Embedding function.
        project_id: Optional project filter.
        similarity_threshold: Minimum similarity score for duplicates.
        limit: Maximum memories to scan.

    Returns:
        List of dicts: {keep_id, delete_id, score, delete_content_preview}.
    """

    scope = _maintenance_scope(project_id)
    memories = storage.list_memories(scope=scope, limit=limit)
    if not memories:
        return []

    duplicates: list[dict[str, Any]] = []
    seen_delete_ids: set[str] = set()

    for i, memory in enumerate(memories):
        if memory.id in seen_delete_ids:
            continue

        try:
            embedding = await embed_fn(memory_embedding_text(memory.content, memory.rationale))
            filters = memory_scope_filter(scope)
            results = await vector_store.search(
                query_embedding=embedding,
                limit=5,
                filters=filters,
            )
        except Exception as e:
            logger.warning("Duplicate scan failed for %s: %s", memory.id, e)
            continue

        for match_id, score in results:
            if match_id == memory.id or match_id in seen_delete_ids:
                continue
            if score < similarity_threshold:
                continue

            # Determine which to keep
            try:
                match = storage.get_memory(match_id)
            except ValueError:
                continue

            # Keep the one with higher access_count; tie-break by updated_at
            if (memory.access_count, memory.updated_at) >= (
                match.access_count,
                match.updated_at,
            ):
                keep, delete = memory, match
            else:
                keep, delete = match, memory

            seen_delete_ids.add(delete.id)
            duplicates.append(
                {
                    "keep_id": keep.id,
                    "delete_id": delete.id,
                    "score": round(score, 4),
                    "delete_content_preview": delete.content[:120],
                }
            )

        # Yield to event loop periodically
        if i % 10 == 9:
            await asyncio.sleep(0)

    return duplicates


def find_code_derivable_memories(
    storage: LocalMemoryManager,
    project_id: str | None = None,
    limit: int = 500,
) -> list[Memory]:
    """Find memories whose content just describes code structure.

    Uses regex heuristics to detect memories like "File X contains function Y"
    that can be re-derived from the codebase. Only flags short memories
    (< _CODE_DERIVABLE_MAX_LEN chars) to avoid false positives on longer
    memories that may contain substantive design context.

    Args:
        storage: Local memory storage.
        project_id: Optional project filter.
        limit: Maximum memories to scan.

    Returns:
        List of code-derivable Memory objects.
    """
    memories = storage.list_memories(scope=_maintenance_scope(project_id), limit=limit)
    results: list[Memory] = []

    for memory in memories:
        content = memory.content.strip()
        if len(content) > _CODE_DERIVABLE_MAX_LEN:
            continue
        if any(pattern.match(content) for pattern in _CODE_DERIVABLE_PATTERNS):
            results.append(memory)

    return results


def find_orphaned_memories(
    db: HubDatabase,
    min_age_days: int = 30,
    project_id: str | None = None,
    limit: int = 500,
) -> list[Memory]:
    """Find memories whose source session no longer exists.

    Only flags orphaned memories that are also old (> min_age_days), since
    a recently created memory whose session was cleaned up is still likely
    valuable.

    Args:
        db: Database connection.
        min_age_days: Only flag orphans older than this many days.
        project_id: Optional project filter.
        limit: Maximum results to return.

    Returns:
        List of orphaned Memory objects.
    """
    from gobby.storage.memories import Memory

    cutoff = (datetime.now(UTC) - timedelta(days=min_age_days)).isoformat()

    params: list[Any] = [cutoff]
    project_clause = ""
    if project_id:
        project_clause = "AND ((m.project_id = %s AND m.is_global IS FALSE) OR m.is_global IS TRUE)"
        params.append(project_id)
    params.append(limit)

    sql = (
        f"SELECT m.* FROM memories m "  # nosec
        "LEFT JOIN sessions s ON m.source_session_id = s.id "
        "WHERE m.source_session_id IS NOT NULL AND s.id IS NULL "
        f"AND m.created_at < %s {project_clause} ORDER BY m.created_at ASC LIMIT %s"
    )
    rows = db.fetchall(sql, tuple(params))

    return [Memory.from_row(row) for row in rows]
