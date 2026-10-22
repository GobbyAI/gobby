"""Task search module using the PostgreSQL keyword backend.

Provides full-text search for tasks using PostgreSQL pg_search BM25 through
the hub database seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.search.keyword import (
    BM25SearchBackend,
    KeywordSearchBackend,
    SearchHit,
    SearchMode,
    pick_search_backend,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

__all__ = [
    "BM25SearchBackend",
    "KeywordSearchBackend",
    "SearchHit",
    "SearchMode",
    "TaskSearchBackend",
    "pick_search_backend",
    "search_tasks",
]


def search_tasks(
    db: HubDatabase,
    query: str,
    *,
    top_k: int = 20,
    project_id: str | None = None,
    task_type: str | None = None,
    priority: int | None = None,
    parent_task_id: str | None = None,
    category: str | None = None,
    min_score: float = 0.0,
) -> list[tuple[str, float]]:
    """Search tasks through the keyword searcher."""
    return TaskSearchBackend(db).search(
        query,
        top_k=top_k,
        project_id=project_id,
        task_type=task_type,
        priority=priority,
        parent_task_id=parent_task_id,
        category=category,
        min_score=min_score,
    )


@dataclass(frozen=True)
class _TaskSearchFilters:
    project_id: str | None = None
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
    """Keyword search for tasks through the shared backend."""

    def __init__(self, db: HubDatabase) -> None:
        self._db = db
        self._backend = pick_search_backend(db, "tasks")

    def search(
        self,
        query: str,
        top_k: int = 20,
        project_id: str | None = None,
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
            task_type=task_type,
            priority=priority,
            parent_task_id=parent_task_id,
            category=category,
        )
        hits = self._backend.search(query, fetch_limit, filters=filters.keyword_filters())

        results: list[tuple[str, float]] = []
        for hit in hits:
            if hit.score < min_score:
                continue
            results.append((hit.id, hit.score))
            if len(results) >= top_k:
                break
        return results

    def reindex(self) -> dict[str, Any]:
        """Return task search backend statistics.

        PostgreSQL pg_search indexes are maintained by the database extension.
        """
        return self.get_stats()

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about the search index."""
        return self._backend.get_stats()
