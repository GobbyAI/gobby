import logging
from collections.abc import Callable
from typing import Any

from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


class MemoryStoreBase:
    def __init__(self, db: HubDatabase):
        self.db = db
        self._change_listeners: list[Callable[[], Any]] = []

    def add_change_listener(self, listener: Callable[[], Any]) -> None:
        self._change_listeners.append(listener)

    def _notify_listeners(self) -> None:
        for listener in self._change_listeners:
            try:
                listener()
            except Exception as e:
                logger.error(f"Error in memory change listener: {e}")

    def restore_memory(self, memory_id: str, when: str | None = None) -> bool:
        raise NotImplementedError
