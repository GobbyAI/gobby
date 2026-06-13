"""Memory keyword search service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gobby.search.keyword import is_pg_search_parse_error, pick_search_backend

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


class MemoryKeywordSearchService:
    """Run memory keyword search through the shared dialect-aware backend."""

    def __init__(self, db: HubDatabase) -> None:
        self._db = db

    def search(
        self,
        query: str,
        limit: int,
        project_id: str | None = None,
    ) -> list[tuple[str, float]]:
        """Return ranked memory IDs for a keyword query."""
        try:
            backend = pick_search_backend(self._db, "memories")
            hits = backend.search(query, limit, filters={"project_id": project_id})
        except Exception as exc:
            if is_pg_search_parse_error(exc):
                logger.debug("Memory keyword query parse failed: %s", exc)
                return []
            raise
        return [(hit.id, hit.score) for hit in hits]
