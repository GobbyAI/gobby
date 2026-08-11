import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from gobby.storage.embedding_generation_state import EmbeddingGenerationState
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)
MEMORY_PROJECTION_FENCE_LOCK_KEY = 4_607_187_217_790_855_201


class MemoryStoreBase:
    def __init__(self, db: HubDatabase) -> None:
        self.db = db
        self.embedding_generation_state = EmbeddingGenerationState(db)
        self._change_listeners: list[Callable[[], Any]] = []

    def add_change_listener(self, listener: Callable[[], Any]) -> None:
        self._change_listeners.append(listener)

    def _notify_listeners(self) -> None:
        for listener in self._change_listeners:
            try:
                listener()
            except Exception:
                logger.exception("Error in memory change listener")

    def notify_changed(self) -> None:
        """Notify listeners after a committed material memory change."""
        self._notify_listeners()

    def restore_memory(self, memory_id: str, when: str | None = None) -> bool:
        raise NotImplementedError

    def mark_dreamed_with_connection(
        self,
        conn: Any,
        memory_id: str,
        *,
        hidden_as: Literal["review", "delete"] | None = None,
        when: datetime | str | None = None,
    ) -> bool:
        raise NotImplementedError
