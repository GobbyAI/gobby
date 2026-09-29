"""Memory maintenance functions: stats and export.

Extracted from manager.py as part of Strangler Fig decomposition (Wave 2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

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
    run_db: Callable[..., Awaitable[Any]] | None = None,
    include_vector_count: bool = True,
) -> dict[str, Any]:
    """Get statistics about stored memories.

    Args:
        storage: Local memory storage manager.
        db: Database connection.
        project_id: Optional project to filter stats by.
        vector_store: Optional VectorStore for vector count stats.
        run_db: Optional managed database offload.
        include_vector_count: Whether to query Qdrant for its live count.

    Returns:
        Dictionary with memory statistics.
    """
    scope = _maintenance_scope(project_id)
    if run_db is None:
        memories = await asyncio.to_thread(storage.list_memories, scope=scope, limit=10000)
    else:
        memories = await run_db(storage.list_memories, scope=scope, limit=10000)

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

    if vector_store is not None and include_vector_count:
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
