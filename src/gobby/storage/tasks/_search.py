"""Task search module using FTS5.

Provides full-text search for tasks using SQLite FTS5 virtual tables.
The tasks_fts table is content-synced with triggers — no manual index
management needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.search.fts5 import FTS5SearchBackend, sanitize_fts_query

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

# bm25 weights: title(10), description(5), labels(2), task_type(1), category(2)
_TASK_BM25_WEIGHTS = (10.0, 5.0, 2.0, 1.0, 2.0)


def search_tasks(
    db: DatabaseProtocol,
    query: str,
    *,
    top_k: int = 20,
    project_id: str | None = None,
    current_stage_state: str | list[str] | None = None,
    task_type: str | None = None,
    priority: int | None = None,
    parent_task_id: str | None = None,
    category: str | None = None,
    min_score: float = 0.0,
) -> list[tuple[str, float]]:
    """Search tasks through the stage-native FTS5 searcher."""
    return TaskFTS5Searcher(db).search(
        query,
        top_k=top_k,
        project_id=project_id,
        current_stage_state=current_stage_state,
        task_type=task_type,
        priority=priority,
        parent_task_id=parent_task_id,
        category=category,
        min_score=min_score,
    )


class TaskFTS5Searcher:
    """FTS5-based search for tasks.

    Uses the tasks_fts virtual table which is kept in sync via triggers.
    All filters are pushed into SQL WHERE clauses for single-query search.
    """

    def __init__(self, db: DatabaseProtocol):
        self._db = db
        self._backend = FTS5SearchBackend(
            db=db,
            fts_table="tasks_fts",
            content_table="tasks",
            id_column="id",
            weights=_TASK_BM25_WEIGHTS,
        )

    def search(
        self,
        query: str,
        top_k: int = 20,
        project_id: str | None = None,
        current_stage_state: str | list[str] | None = None,
        task_type: str | None = None,
        priority: int | None = None,
        parent_task_id: str | None = None,
        category: str | None = None,
        min_score: float = 0.0,
    ) -> list[tuple[str, float]]:
        """Search tasks with FTS5 and SQL filter push-down.

        Args:
            query: Search query text
            top_k: Maximum number of results
            project_id: Filter by project
            current_stage_state: Filter by current stage state (string or list)
            task_type: Filter by task type
            priority: Filter by priority
            parent_task_id: Filter by parent task ID (UUID)
            category: Filter by category
            min_score: Minimum normalized score threshold (0.0-1.0)

        Returns:
            List of (task_id, normalized_score) tuples, highest score first.
        """
        fts_query = sanitize_fts_query(query)
        if not fts_query:
            return []

        weights_csv = ", ".join(str(w) for w in _TASK_BM25_WEIGHTS)
        bm25_expr = f"bm25(tasks_fts, {weights_csv})"

        conditions = ["tasks_fts MATCH ?"]
        params: list[Any] = [fts_query]

        if project_id:
            conditions.append("t.project_id = ?")
            params.append(project_id)

        if current_stage_state:
            raw_states = (
                [current_stage_state]
                if isinstance(current_stage_state, str)
                else list(current_stage_state)
            )
            states = [
                str(state).strip().lower().replace("-", "_")
                for state in raw_states
                if str(state).strip()
            ]
            if states:
                placeholders = ", ".join("?" for _ in states)
                stage_clauses = [
                    f"""
                    EXISTS (
                        SELECT 1
                          FROM task_stage_states current_stage
                         WHERE current_stage.task_id = t.id
                           AND current_stage.state != 'done'
                           AND current_stage.position = (
                               SELECT MIN(stage_scan.position)
                                 FROM task_stage_states stage_scan
                                WHERE stage_scan.task_id = t.id
                                  AND stage_scan.state != 'done'
                           )
                           AND current_stage.state IN ({placeholders})
                    )
                    """
                ]
                if "ready" in states:
                    stage_clauses.append(
                        """
                        NOT EXISTS (
                            SELECT 1
                              FROM task_stage_states stage_any
                             WHERE stage_any.task_id = t.id
                        )
                        """
                    )
                conditions.append(f"({' OR '.join(stage_clauses)})")
                params.extend(states)
                conditions.append("t.closed_at IS NULL")

        if task_type:
            conditions.append("t.task_type = ?")
            params.append(task_type)

        if priority is not None:
            conditions.append("t.priority = ?")
            params.append(priority)

        if parent_task_id:
            conditions.append("t.parent_task_id = ?")
            params.append(parent_task_id)

        if category:
            conditions.append("t.category = ?")
            params.append(category)

        where = " AND ".join(conditions)
        # Fetch extra to allow for min_score filtering
        fetch_limit = top_k * 3 if min_score > 0 else top_k
        params.append(fetch_limit)

        sql = f"""
            SELECT t.id, {bm25_expr} as rank
            FROM tasks_fts fts
            JOIN tasks t ON t.rowid = fts.rowid
            WHERE {where}
            ORDER BY rank
            LIMIT ?
        """

        try:
            rows = self._db.fetchall(sql, tuple(params))
        except Exception as e:
            logger.warning(f"FTS5 task search failed: {e}")
            return []

        if not rows:
            return []

        # Normalize scores
        ids = [str(row["id"]) for row in rows]
        raw_scores = [float(row["rank"]) for row in rows]
        positive = [-s for s in raw_scores]
        max_score = max(positive) if positive else 1.0
        if max_score == 0:
            normalized = [0.0] * len(positive)
        else:
            normalized = [s / max_score for s in positive]

        # Apply min_score filter and limit
        results: list[tuple[str, float]] = []
        for task_id, score in zip(ids, normalized, strict=False):
            if score < min_score:
                continue
            results.append((task_id, score))
            if len(results) >= top_k:
                break

        return results

    def reindex(self) -> dict[str, Any]:
        """Rebuild the FTS5 index from the tasks table.

        Useful for repair — normally triggers keep the index in sync.

        Returns:
            Dict with index statistics.
        """
        try:
            self._db.execute("DELETE FROM tasks_fts")
            self._db.execute("""
                INSERT INTO tasks_fts(rowid, title, description, labels, task_type, category)
                SELECT rowid, title, description, labels, task_type, category FROM tasks
            """)
        except Exception as e:
            logger.error(f"Failed to reindex tasks_fts: {e}")

        return self.get_stats()

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about the search index."""
        return self._backend.get_stats()
