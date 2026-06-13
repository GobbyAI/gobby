"""Thread manager for communication channels."""

from __future__ import annotations

from collections import OrderedDict


class ThreadManager:
    """Manages mapping between Gobby sessions and platform thread IDs with LRU eviction."""

    def __init__(self, max_size: int = 10000) -> None:
        """Initialize the thread manager.

        Args:
            max_size: Maximum number of entries to track before evicting oldest.
        """
        self._max_size = max_size
        self._thread_map: OrderedDict[tuple[str, str], str] = (
            OrderedDict()
        )  # (internal channel_id, session_id) -> platform_thread_id

    def get_thread_id(self, channel_id: str, session_id: str) -> str | None:
        """Get the platform thread ID for a session on a channel."""
        key = (channel_id, session_id)
        thread_id = self._thread_map.get(key)
        if thread_id is not None:
            self._thread_map.move_to_end(key)  # Mark as recently used
        return thread_id

    def track_thread(self, channel_id: str, session_id: str, platform_thread_id: str) -> None:
        """Track a platform thread ID for a session on a channel."""
        key = (channel_id, session_id)
        self._thread_map[key] = platform_thread_id
        self._thread_map.move_to_end(key)
        while len(self._thread_map) > self._max_size:
            self._thread_map.popitem(last=False)  # Evict least recently used
