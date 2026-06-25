"""Memory access-stat updates after search result delivery."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gobby.storage.memories import LocalMemoryManager, Memory

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig

logger = logging.getLogger(__name__)


def update_access_stats(
    *,
    storage: LocalMemoryManager,
    config: MemoryConfig,
    memories: list[Memory],
) -> None:
    """Update access count and time for memories with debounce protection."""
    if not memories:
        return

    now = datetime.now(UTC)
    debounce_seconds = getattr(config, "access_debounce_seconds", 60)

    for memory in memories:
        if memory.last_accessed_at:
            try:
                last_access = datetime.fromisoformat(memory.last_accessed_at)
                if last_access.tzinfo is None:
                    last_access = last_access.replace(tzinfo=UTC)
                seconds_since = (now - last_access).total_seconds()
                if seconds_since < debounce_seconds:
                    continue
            except (ValueError, TypeError):
                pass

        try:
            storage.update_access_stats(memory.id, now.isoformat())
        except Exception as exc:
            if "malformed" in str(exc):
                logger.warning(
                    "Failed to update access stats for %s: %s "
                    "(likely FTS trigger issue - see memory FTS repair docs)",
                    memory.id,
                    exc,
                )
            else:
                logger.warning("Failed to update access stats for %s: %s", memory.id, exc)
