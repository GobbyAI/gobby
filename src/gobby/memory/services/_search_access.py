"""Surfaced-stat updates after search result delivery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gobby.storage.memories import LocalMemoryManager, Memory
from gobby.utils.datetime import parse_stored_datetime, utc_now

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig

logger = logging.getLogger(__name__)

# Searches whose hits are shown to an agent or user. Every other caller -- the
# `memory.search` default, the create_memory similarity probe, and review-lesson
# lookups -- only inspects candidates, so its hits are not counted as surfaced.
SURFACED_CALLERS = frozenset(
    {
        "memory.surface",
        "mcp_proxy.memory.search_memories",
        "mcp_proxy.memory.review_task_memories",
        "http.memory.search",
        "cli.memory.recall",
    }
)


def update_surfaced_stats(
    *,
    storage: LocalMemoryManager,
    config: MemoryConfig,
    memories: list[Memory],
) -> None:
    """Count each delivered search hit as surfaced, with debounce protection."""
    if not memories:
        return

    now = utc_now()
    debounce_seconds = getattr(config, "access_debounce_seconds", 60)

    for memory in memories:
        try:
            last_surfaced_at = parse_stored_datetime(memory.last_surfaced_at)
        except ValueError:
            logger.warning("Skipping surfaced stats for %s: invalid timestamp", memory.id)
            continue

        if last_surfaced_at is not None:
            seconds_since = (now - last_surfaced_at).total_seconds()
            if seconds_since < debounce_seconds:
                continue

        try:
            storage.update_surfaced_stats(memory.id, now)
        except Exception as exc:
            logger.warning("Failed to update surfaced stats for %s: %s", memory.id, exc)
