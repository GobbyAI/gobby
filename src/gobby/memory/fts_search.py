"""FTS5-based keyword search for memories.

Provides full-text search fallback when vector search returns no results
above the similarity threshold. Uses the memories_fts virtual table which
is kept in sync via triggers (see migrations.py _setup_memories_fts).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.search.fts5 import sanitize_fts_query

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

# bm25 weights: content(10), tags(5), memory_type(1), source_type(1)
_MEMORY_BM25_WEIGHTS = (10.0, 5.0, 1.0, 1.0)


class MemoryFTS5Searcher:
    """FTS5-based keyword search for memories.

    Uses the memories_fts virtual table which is kept in sync via triggers.
    Designed as a fallback when semantic vector search returns no results
    above the relevance threshold.
    """

    def __init__(self, db: DatabaseProtocol) -> None:
        self._db = db

    def clear(self) -> None:
        """Clear the FTS5 index using the database transaction boundary."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM memories_fts")

    def search(
        self,
        query: str,
        top_k: int = 10,
        project_id: str | None = None,
    ) -> list[tuple[str, float]]:
        """Search memories with FTS5 keyword matching.

        Args:
            query: Search query text
            top_k: Maximum number of results
            project_id: Filter by project

        Returns:
            List of (memory_id, normalized_score) tuples, highest score first.
        """
        fts_query = sanitize_fts_query(query)
        if not fts_query:
            return []

        weights_csv = ", ".join(str(w) for w in _MEMORY_BM25_WEIGHTS)
        bm25_expr = f"bm25(memories_fts, {weights_csv})"

        conditions = ["memories_fts MATCH ?"]
        params: list[Any] = [fts_query]

        if project_id:
            conditions.append("m.project_id = ?")
            params.append(project_id)

        where = " AND ".join(conditions)
        params.append(top_k)

        sql = f"""
            SELECT m.id, {bm25_expr} as rank
            FROM memories_fts fts
            JOIN memories m ON m.rowid = fts.rowid
            WHERE {where}
            ORDER BY rank
            LIMIT ?
        """

        try:
            rows = self._db.fetchall(sql, tuple(params))
        except Exception as e:
            logger.warning(f"FTS5 memory search failed: {e}")
            return []

        if not rows:
            return []

        # Normalize scores: bm25 returns negative (more negative = better)
        ids = [str(row[0]) for row in rows]
        raw_scores = [float(row[1]) for row in rows]
        positive = [-s for s in raw_scores]
        max_score = max(positive) if positive else 1.0
        if max_score == 0:
            normalized = [0.0] * len(positive)
        else:
            normalized = [s / max_score for s in positive]

        return list(zip(ids, normalized, strict=False))

    def reindex(self) -> dict[str, Any]:
        """Rebuild the FTS5 index from the memories table.

        Useful for repair — normally triggers keep the index in sync.

        Returns:
            Dict with reindex result.
        """
        try:
            with self._db.transaction() as conn:
                conn.execute("DELETE FROM memories_fts")
                conn.execute("""
                    INSERT INTO memories_fts(rowid, content, tags, memory_type, source_type)
                    SELECT rowid, content,
                           REPLACE(REPLACE(REPLACE(COALESCE(tags, ''), '"', ''), '[', ''), ']', ''),
                           memory_type, COALESCE(source_type, '')
                    FROM memories
                """)
                row = conn.execute("SELECT count(*) FROM memories_fts").fetchone()
            count = row[0] if row else 0
        except Exception as e:
            logger.error(f"Failed to reindex memories_fts: {e}")
            return {"success": False, "error": str(e)}

        return {"success": True, "indexed": count}
