"""Task search module using dialect-aware keyword backends.

Provides full-text search for tasks using SQLite FTS5 or PostgreSQL pg_search
BM25 through the hub database seam.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.search.keyword import (
    BM25SearchBackend,
    FTS5SearchBackend,
    KeywordSearchBackend,
    SearchHit,
    SearchMode,
    fetch_all,
    normalize_fts5_scores,
    normalize_positive_scores,
    pick_search_backend,
    placeholder,
    row_value,
    sanitize_pg_search_query,
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
    "TaskSearchBackend",
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
    """Search tasks through the dialect-aware keyword searcher."""
    return TaskSearchBackend(db).search(
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


@dataclass(frozen=True)
class _TaskSearchFilters:
    project_id: str | None = None
    current_stage_state: str | list[str] | None = None
    task_type: str | None = None
    priority: int | None = None
    parent_task_id: str | None = None
    category: str | None = None

    def keyword_filters(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "task_type": self.task_type,
            "priority": self.priority,
            "parent_task_id": self.parent_task_id,
            "category": self.category,
        }


class TaskSearchBackend:
    """Dialect-aware keyword search for tasks.

    Uses the shared keyword backend for normal filters. Stage-state filtering
    needs a task-specific query because the current stage is derived from
    `task_stage_states`.
    """

    def __init__(self, db: DatabaseProtocol) -> None:
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
        """Search tasks with SQL filter push-down.

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
        filters = _TaskSearchFilters(
            project_id=project_id,
            current_stage_state=current_stage_state,
            task_type=task_type,
            priority=priority,
            parent_task_id=parent_task_id,
            category=category,
        )
        if filters.current_stage_state:
            hits = self._search_with_stage_state(
                query=query,
                limit=fetch_limit,
                filters=filters,
            )
        else:
            hits = self._backend.search(query, fetch_limit, filters=filters.keyword_filters())

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
        filters: _TaskSearchFilters,
    ) -> list[SearchHit]:
        stage_state = filters.current_stage_state
        if stage_state is None:
            return []
        if getattr(self._db, "dialect", "sqlite") == "postgres":
            return self._search_postgres_with_stage_state(
                query=query,
                limit=limit,
                filters=filters,
                current_stage_state=stage_state,
            )
        return self._search_sqlite_with_stage_state(
            query=query,
            limit=limit,
            filters=filters,
            current_stage_state=stage_state,
        )

    def _search_sqlite_with_stage_state(
        self,
        *,
        query: str,
        limit: int,
        filters: _TaskSearchFilters,
        current_stage_state: str | list[str],
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
            filters=filters,
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
        scores = normalize_fts5_scores([float(row_value(row, "score")) for row in rows])
        return [
            SearchHit(id=str(row_value(row, "id")), score=score)
            for row, score in zip(rows, scores, strict=False)
        ]

    def _search_postgres_with_stage_state(
        self,
        *,
        query: str,
        limit: int,
        filters: _TaskSearchFilters,
        current_stage_state: str | list[str],
    ) -> list[SearchHit]:
        bm25_query = sanitize_pg_search_query(query)
        if not bm25_query:
            return []

        params: list[Any] = []
        query_placeholder = self._add_param(params, bm25_query)
        conditions = [f"(t.title @@@ {query_placeholder} OR t.description @@@ {query_placeholder})"]
        self._append_common_filters(
            params=params,
            conditions=conditions,
            filters=filters,
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
        scores = normalize_positive_scores([float(row_value(row, "score")) for row in rows])
        return [
            SearchHit(id=str(row_value(row, "id")), score=score)
            for row, score in zip(rows, scores, strict=False)
        ]

    def _append_common_filters(
        self,
        *,
        params: list[Any],
        conditions: list[str],
        filters: _TaskSearchFilters,
    ) -> None:
        if filters.project_id:
            conditions.append(f"t.project_id = {self._add_param(params, filters.project_id)}")
        if filters.task_type:
            conditions.append(f"t.task_type = {self._add_param(params, filters.task_type)}")
        if filters.priority is not None:
            conditions.append(f"t.priority = {self._add_param(params, filters.priority)}")
        if filters.parent_task_id:
            conditions.append(
                f"t.parent_task_id = {self._add_param(params, filters.parent_task_id)}"
            )
        if filters.category:
            conditions.append(f"t.category = {self._add_param(params, filters.category)}")

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


TaskFTS5Searcher = TaskSearchBackend
