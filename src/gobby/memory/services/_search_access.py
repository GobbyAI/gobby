"""Memory access-stat updates after search result delivery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gobby.storage.memories import LocalMemoryManager, Memory
from gobby.utils.datetime import parse_stored_datetime, utc_now

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

    now = utc_now()
    debounce_seconds = getattr(config, "access_debounce_seconds", 60)

    for memory in memories:
        try:
            last_accessed_at = parse_stored_datetime(memory.last_accessed_at)
        except ValueError:
            logger.warning("Skipping access stats for %s: invalid timestamp", memory.id)
            continue

        if last_accessed_at is not None:
            seconds_since = (now - last_accessed_at).total_seconds()
            if seconds_since < debounce_seconds:
                continue

        try:
            storage.update_access_stats(memory.id, now)
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
