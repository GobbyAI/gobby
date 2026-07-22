"""Memory keyword search service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.search.keyword import (
    MemoryKeywordScope,
    SearchHit,
    is_pg_search_parse_error,
    map_keyword_search_rows,
    pick_search_backend,
    render_keyword_search_statement,
)

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
        *,
        include_global: bool = True,
        scope: MemoryKeywordScope | None = None,
    ) -> list[tuple[str, float]]:
        """Return ranked memory IDs for a keyword query."""
        try:
            backend = pick_search_backend(self._db, "memories")
            hits = backend.search(
                query,
                limit,
                filters=self._filters(project_id, include_global=include_global, scope=scope),
            )
        except Exception as exc:
            if is_pg_search_parse_error(exc):
                logger.debug("Memory keyword query parse failed: %s", exc)
                return []
            raise
        return [(hit.id, hit.score) for hit in hits]

    def render_search(
        self,
        query: str,
        limit: int,
        project_id: str | None = None,
        *,
        include_global: bool = True,
        scope: MemoryKeywordScope | None = None,
    ) -> tuple[str, tuple[Any, ...]] | None:
        """Render the statement used by the synchronous search surface."""
        return render_keyword_search_statement(
            self._db,
            "memories",
            query,
            limit,
            filters=self._filters(project_id, include_global=include_global, scope=scope),
        )

    @staticmethod
    def map_rows(rows: list[Any]) -> list[SearchHit]:
        """Map rows returned by either sync or dedicated async connections."""
        return map_keyword_search_rows(rows)

    @staticmethod
    def _filters(
        project_id: str | None,
        *,
        include_global: bool,
        scope: MemoryKeywordScope | None,
    ) -> dict[str, Any]:
        if scope is not None:
            return {"memory_scope": scope, "project_id": project_id}
        return {
            "project_id": project_id,
            "include_global": include_global,
            **({"is_global": False} if project_id and not include_global else {}),
        }
