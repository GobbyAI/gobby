"""FTS5-based search backend using SQLite full-text search.

Provides an AsyncSearchBackend implementation backed by SQLite FTS5 virtual
tables. Used for task search (content-synced with triggers) and as the
keyword fallback for skill search (contentless, app-managed).

Score normalization converts FTS5 bm25() scores (negative, lower=better)
to [0, 1] range (higher=better) for API compatibility.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


def sanitize_fts_query(query: str) -> str:
    """Sanitize user input for FTS5 queries.

    Strips FTS5 special characters and joins tokens with implicit AND.
    Each token is quoted to prevent FTS5 syntax errors.

    Args:
        query: Raw user search query

    Returns:
        Sanitized FTS5 query string, or empty string if no valid tokens.
    """
    cleaned = ""
    for ch in query:
        if ch.isalnum() or ch in (" ", "_", "-"):
            cleaned += ch
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        return ""
    return " ".join(f'"{t}"' for t in tokens)


def _normalize_scores(raw_scores: list[float]) -> list[float]:
    """Convert FTS5 bm25 scores to [0, 1] range.

    bm25() returns negative floats where more-negative = better match.
    We negate to make higher = better, then divide by max for [0, 1].

    Args:
        raw_scores: Raw bm25 scores from FTS5

    Returns:
        Normalized scores in [0, 1] where 1.0 = best match in result set.
    """
    if not raw_scores:
        return []
    positive = [-s for s in raw_scores]
    max_score = max(positive) if positive else 1.0
    if max_score == 0:
        return [0.0] * len(positive)
    return [s / max_score for s in positive]


class FTS5SearchBackend:
    """FTS5-based search backend implementing AsyncSearchBackend protocol.

    Configurable for different FTS5 tables. Supports both content-synced
    tables (task search — triggers handle sync) and contentless tables
    (skill search — application manages inserts/deletes).

    Args:
        db: LocalDatabase instance
        fts_table: Name of the FTS5 virtual table (e.g., 'tasks_fts')
        content_table: Name of the content table to JOIN for ID retrieval
            (e.g., 'tasks'). Set to None for contentless tables.
        id_column: Column name for the item ID in the content table (e.g., 'id')
        weights: bm25 column weights (one per FTS5 column, higher = more important).
            Order must match the FTS5 table column order.
    """

    def __init__(
        self,
        db: DatabaseProtocol,
        fts_table: str,
        content_table: str | None,
        id_column: str = "id",
        weights: tuple[float, ...] | None = None,
    ):
        self._db = db
        self._fts_table = fts_table
        self._content_table = content_table
        self._id_column = id_column
        self._weights = weights
        self._fitted = False

    async def fit_async(self, items: list[tuple[str, str]]) -> None:
        """No-op for content-synced tables. For contentless tables, this
        is handled by the domain-specific indexer (e.g., SkillSearch)."""
        self._fitted = True

    async def search_async(
        self,
        query: str,
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """Search using FTS5 MATCH with bm25 ranking.

        Args:
            query: Search query text
            top_k: Maximum number of results

        Returns:
            List of (item_id, normalized_score) tuples, highest score first.
        """
        fts_query = sanitize_fts_query(query)
        if not fts_query:
            return []

        weights_csv = ", ".join(str(w) for w in self._weights) if self._weights else ""
        bm25_expr = (
            f"bm25({self._fts_table}, {weights_csv})" if weights_csv else f"bm25({self._fts_table})"
        )

        if self._content_table:
            sql = f"""
                SELECT ct.{self._id_column}, {bm25_expr} as rank
                FROM {self._fts_table} fts
                JOIN {self._content_table} ct ON ct.rowid = fts.rowid
                WHERE {self._fts_table} MATCH ?
                ORDER BY rank
                LIMIT ?
            """
        else:
            # Contentless table — rowid is the only join key available.
            # Caller must map rowids to IDs externally.
            sql = f"""
                SELECT fts.rowid AS id, {bm25_expr} as rank
                FROM {self._fts_table} fts
                WHERE {self._fts_table} MATCH ?
                ORDER BY rank
                LIMIT ?
            """

        try:
            rows = self._db.fetchall(sql, (fts_query, top_k))
        except Exception as e:
            logger.debug(f"FTS5 search failed on {self._fts_table}: {e}")
            return []

        if not rows:
            return []

        ids = [str(row["id"]) for row in rows]
        raw_scores = [float(row["rank"]) for row in rows]
        normalized = _normalize_scores(raw_scores)

        return list(zip(ids, normalized, strict=False))

    def search(
        self,
        query: str,
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """Synchronous search wrapper.

        Args:
            query: Search query text
            top_k: Maximum number of results

        Returns:
            List of (item_id, normalized_score) tuples, highest score first.
        """
        import asyncio

        return asyncio.get_event_loop().run_until_complete(self.search_async(query, top_k))

    def needs_refit(self) -> bool:
        """FTS5 tables are always in sync (triggers) or app-managed."""
        return False

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about the FTS5 index."""
        try:
            row = self._db.fetchone(
                f"SELECT count(*) as cnt FROM {self._fts_table}",
            )
            count = row["cnt"] if row else 0
        except Exception:
            count = 0

        return {
            "backend_type": "fts5",
            "fts_table": self._fts_table,
            "content_table": self._content_table,
            "document_count": count,
            "fitted": self._fitted,
        }

    def clear(self) -> None:
        """Clear the FTS5 index."""
        try:
            self._db.execute(f"DELETE FROM {self._fts_table}")
        except Exception as e:
            logger.warning(f"Failed to clear {self._fts_table}: {e}")
        self._fitted = False
