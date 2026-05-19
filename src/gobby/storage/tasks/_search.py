"""Task search module using dialect-aware keyword backends.

Provides full-text search for tasks using SQLite FTS5 or PostgreSQL pg_search
BM25 through the hub database seam.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.search.keyword import (
    BM25SearchBackend,
    FTS5SearchBackend,
    KeywordSearchBackend,
    SearchHit,
    SearchMode,
    fetch_all,
    pick_search_backend,
    placeholder,
    row_value,
)

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

__all__ = [
    "BM25SearchBackend",
    "FTS5SearchBackend",
    "KeywordSearchBackend",
    "SearchHit",
    "SearchMode",
    "TaskFTS5Searcher",
    "pick_search_backend",
    "search_tasks",
]


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
        self._backend = pick_search_backend(db, "tasks")

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
        fetch_limit = top_k * 3 if min_score > 0 else top_k
        if current_stage_state:
            hits = self._search_with_stage_state(
                query=query,
                limit=fetch_limit,
                project_id=project_id,
                current_stage_state=current_stage_state,
                task_type=task_type,
                priority=priority,
                parent_task_id=parent_task_id,
                category=category,
            )
        else:
            filters = {
                "project_id": project_id,
                "task_type": task_type,
                "priority": priority,
                "parent_task_id": parent_task_id,
                "category": category,
            }
            hits = self._backend.search(query, fetch_limit, filters=filters)

        results: list[tuple[str, float]] = []
        for hit in hits:
            if hit.score < min_score:
                continue
            results.append((hit.id, hit.score))
            if len(results) >= top_k:
                break
        return results

    def _search_with_stage_state(
        self,
        *,
        query: str,
        limit: int,
        project_id: str | None,
        current_stage_state: str | list[str],
        task_type: str | None,
        priority: int | None,
        parent_task_id: str | None,
        category: str | None,
    ) -> list[SearchHit]:
        if getattr(self._db, "dialect", "sqlite") == "postgres":
            return self._search_postgres_with_stage_state(
                query=query,
                limit=limit,
                project_id=project_id,
                current_stage_state=current_stage_state,
                task_type=task_type,
                priority=priority,
                parent_task_id=parent_task_id,
                category=category,
            )
        return self._search_sqlite_with_stage_state(
            query=query,
            limit=limit,
            project_id=project_id,
            current_stage_state=current_stage_state,
            task_type=task_type,
            priority=priority,
            parent_task_id=parent_task_id,
            category=category,
        )

    def _search_sqlite_with_stage_state(
        self,
        *,
        query: str,
        limit: int,
        project_id: str | None,
        current_stage_state: str | list[str],
        task_type: str | None,
        priority: int | None,
        parent_task_id: str | None,
        category: str | None,
    ) -> list[SearchHit]:
        from gobby.search.fts5 import sanitize_fts_query

        fts_query = sanitize_fts_query(query)
        if not fts_query:
            return []

        params: list[Any] = []
        conditions = [f"tasks_fts MATCH {self._add_param(params, fts_query)}"]
        self._append_common_filters(
            params=params,
            conditions=conditions,
            project_id=project_id,
            task_type=task_type,
            priority=priority,
            parent_task_id=parent_task_id,
            category=category,
        )
        self._append_stage_filter(params, conditions, current_stage_state)
        limit_placeholder = self._add_param(params, limit)

        sql = f"""
            SELECT t.id AS id,
                   bm25(tasks_fts, 10.0, 5.0, 2.0, 1.0, 2.0) AS score
              FROM tasks_fts fts
              JOIN tasks t ON t.rowid = fts.rowid
             WHERE {" AND ".join(conditions)}
             ORDER BY score ASC, id ASC
             LIMIT {limit_placeholder}
        """
        try:
            rows = fetch_all(self._db, sql, params)
        except Exception as e:
            logger.warning(f"FTS5 task search failed: {e}")
            return []
        scores = _normalize_fts_scores([float(row_value(row, "score")) for row in rows])
        return [
            SearchHit(id=str(row_value(row, "id")), score=score)
            for row, score in zip(rows, scores, strict=False)
        ]

    def _search_postgres_with_stage_state(
        self,
        *,
        query: str,
        limit: int,
        project_id: str | None,
        current_stage_state: str | list[str],
        task_type: str | None,
        priority: int | None,
        parent_task_id: str | None,
        category: str | None,
    ) -> list[SearchHit]:
        bm25_query = " ".join(ch if ch.isalnum() or ch in (" ", "_", "-") else " " for ch in query)
        bm25_query = " ".join(bm25_query.split())
        if not bm25_query:
            return []

        params: list[Any] = []
        query_placeholder = self._add_param(params, bm25_query)
        conditions = [f"(t.title @@@ {query_placeholder} OR t.description @@@ {query_placeholder})"]
        self._append_common_filters(
            params=params,
            conditions=conditions,
            project_id=project_id,
            task_type=task_type,
            priority=priority,
            parent_task_id=parent_task_id,
            category=category,
        )
        self._append_stage_filter(params, conditions, current_stage_state)
        limit_placeholder = self._add_param(params, limit)

        sql = f"""
            SELECT t.id AS id, pdb.score(t.id) AS score
              FROM tasks t
             WHERE {" AND ".join(conditions)}
             ORDER BY score DESC, id ASC
             LIMIT {limit_placeholder}
        """
        try:
            rows = fetch_all(self._db, sql, params)
        except Exception as e:
            logger.warning(f"pg_search task search failed: {e}")
            return []
        scores = _normalize_positive_scores([float(row_value(row, "score")) for row in rows])
        return [
            SearchHit(id=str(row_value(row, "id")), score=score)
            for row, score in zip(rows, scores, strict=False)
        ]

    def _append_common_filters(
        self,
        *,
        params: list[Any],
        conditions: list[str],
        project_id: str | None,
        task_type: str | None,
        priority: int | None,
        parent_task_id: str | None,
        category: str | None,
    ) -> None:
        if project_id:
            conditions.append(f"t.project_id = {self._add_param(params, project_id)}")
        if task_type:
            conditions.append(f"t.task_type = {self._add_param(params, task_type)}")
        if priority is not None:
            conditions.append(f"t.priority = {self._add_param(params, priority)}")
        if parent_task_id:
            conditions.append(f"t.parent_task_id = {self._add_param(params, parent_task_id)}")
        if category:
            conditions.append(f"t.category = {self._add_param(params, category)}")

    def _append_stage_filter(
        self,
        params: list[Any],
        conditions: list[str],
        current_stage_state: str | list[str],
    ) -> None:
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
        if not states:
            return
        state_placeholders = ", ".join(self._add_param(params, state) for state in states)
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
                   AND current_stage.state IN ({state_placeholders})
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
        conditions.append("t.closed_at IS NULL")

    def _add_param(self, params: list[Any], value: Any) -> str:
        params.append(value)
        return placeholder(self._db, len(params))

    def reindex(self) -> dict[str, Any]:
        """Rebuild the FTS5 index from the tasks table.

        Useful for repair — normally triggers keep the index in sync.

        Returns:
            Dict with index statistics.
        """
        if getattr(self._db, "dialect", "sqlite") != "sqlite":
            return self.get_stats()
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


def _normalize_fts_scores(raw_scores: list[float]) -> list[float]:
    return _normalize_positive_scores([-score for score in raw_scores])


def _normalize_positive_scores(raw_scores: list[float]) -> list[float]:
    if not raw_scores:
        return []
    max_score = max(raw_scores)
    if max_score <= 0:
        return [0.0] * len(raw_scores)
    return [score / max_score for score in raw_scores]
