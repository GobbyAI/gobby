"""Tests for task search functionality."""

from typing import Any

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.tasks import LocalTaskManager
from tests.storage.tasks._stage_test_helpers import set_stage_state

pytestmark = pytest.mark.unit


@pytest.fixture
def db_with_tasks(tmp_path):
    """Create a database with some tasks for testing."""
    db_path = tmp_path / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)

    # Create project
    project_id = "test-project-id"
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO projects (id, name, repo_path, created_at, updated_at)
            VALUES (?, ?, ?, datetime('now'), datetime('now'))
            """,
            (project_id, "Test Project", str(tmp_path)),
        )

    manager = LocalTaskManager(db)

    # Create diverse tasks for search testing
    manager.create_task(
        project_id=project_id,
        title="Implement user authentication with JWT",
        description="Add JWT-based authentication to the API endpoints",
        task_type="feature",
        priority=1,
        labels=["auth", "security"],
    )

    manager.create_task(
        project_id=project_id,
        title="Fix database connection timeout",
        description="The database connection pool is timing out under load",
        task_type="bug",
        priority=1,
        labels=["database", "performance"],
    )

    manager.create_task(
        project_id=project_id,
        title="Add user profile page",
        description="Create a profile page where users can update their settings",
        task_type="feature",
        priority=2,
        labels=["ui", "user"],
    )

    manager.create_task(
        project_id=project_id,
        title="Refactor authentication middleware",
        description="Clean up the authentication middleware for better maintainability",
        task_type="task",
        priority=3,
        labels=["auth", "refactor"],
    )

    manager.create_task(
        project_id=project_id,
        title="Update documentation for API",
        description="Write comprehensive API documentation",
        task_type="task",
        priority=2,
        labels=["docs"],
    )

    return db, manager, project_id


class TestTaskSearch:
    """Tests for LocalTaskManager.search_tasks method."""

    def test_search_returns_relevant_results(self, db_with_tasks) -> None:
        """Test that search returns tasks matching the query."""
        db, manager, project_id = db_with_tasks

        results = manager.search_tasks("authentication", project_id=project_id)

        assert len(results) > 0
        # Check that auth-related tasks are in results
        titles = [task.title for task, score in results]
        assert any("authentication" in t.lower() for t in titles)

    def test_search_with_current_stage_state_filter(self, db_with_tasks) -> None:
        """Test search with current stage state filter."""
        db, manager, project_id = db_with_tasks

        # All default task manifests start with development ready.
        results = manager.search_tasks(
            "authentication",
            project_id=project_id,
            current_stage_state="ready",
        )
        assert len(results) > 0

        # No tasks are review-approved until their current stage is advanced.
        results = manager.search_tasks(
            "authentication",
            project_id=project_id,
            current_stage_state="review_approved",
        )
        assert len(results) == 0

    def test_search_current_stage_state_filter_uses_stage_manifest(self, db_with_tasks) -> None:
        """Stage-state filtering should follow the current stage manifest row."""
        db, manager, project_id = db_with_tasks

        task = manager.create_task(
            project_id=project_id,
            title="Authentication review gate",
            description="Authentication task waiting on approval",
        )
        set_stage_state(db, task.id, "development", "review_approved")

        approved = manager.search_tasks(
            "Authentication review gate",
            project_id=project_id,
            current_stage_state="review_approved",
        )
        ready_results = manager.search_tasks(
            "Authentication review gate",
            project_id=project_id,
            current_stage_state="ready",
        )

        assert any(found.id == task.id for found, _ in approved)
        assert all(found.id != task.id for found, _ in ready_results)

    def test_search_current_stage_state_filter_excludes_stale_closed_task(
        self, db_with_tasks
    ) -> None:
        """FTS current-stage filtering should not return closed tasks by stale stage row."""
        db, manager, project_id = db_with_tasks

        open_task = manager.create_task(
            project_id=project_id,
            title="Authentication review active",
            description="Authentication review shared marker",
        )
        set_stage_state(db, open_task.id, "development", "needs_review")
        closed_task = manager.create_task(
            project_id=project_id,
            title="Authentication review closed",
            description="Authentication review shared marker",
        )
        set_stage_state(db, closed_task.id, "development", "needs_review")
        db.execute(
            """
            UPDATE tasks
               SET closed_at = ?,
                   closed_reason = ?
             WHERE id = ?
            """,
            ("2026-05-06T00:00:00+00:00", "closed-with-stale-stage", closed_task.id),
        )

        results = manager.search_tasks(
            "Authentication review shared marker",
            project_id=project_id,
            current_stage_state="needs_review",
        )
        result_ids = {task.id for task, _score in results}

        assert open_task.id in result_ids
        assert closed_task.id not in result_ids

    def test_search_with_current_stage_state_list(self, db_with_tasks) -> None:
        """Test search with list of current stage states."""
        db, manager, project_id = db_with_tasks

        results = manager.search_tasks(
            "user",
            project_id=project_id,
            current_stage_state=["ready", "in_progress"],
        )
        assert len(results) > 0

    def test_search_with_task_type_filter(self, db_with_tasks) -> None:
        """Test search with task type filter."""
        db, manager, project_id = db_with_tasks

        results = manager.search_tasks(
            "database",
            project_id=project_id,
            task_type="bug",
        )

        assert len(results) > 0
        for task, _score in results:
            assert task.task_type == "bug"

    def test_search_with_priority_filter(self, db_with_tasks) -> None:
        """Test search with priority filter."""
        db, manager, project_id = db_with_tasks

        results = manager.search_tasks(
            "user",
            project_id=project_id,
            priority=2,
        )

        for task, _score in results:
            assert task.priority == 2

    def test_search_with_min_score(self, db_with_tasks) -> None:
        """Test search with minimum score threshold."""
        db, manager, project_id = db_with_tasks

        # First get all results
        all_results = manager.search_tasks(
            "user",
            project_id=project_id,
            min_score=0.0,
        )

        # Then filter by min_score
        filtered_results = manager.search_tasks(
            "user",
            project_id=project_id,
            min_score=0.1,
        )

        # Filtered should have same or fewer results
        assert len(filtered_results) <= len(all_results)

        # All filtered results should have score >= 0.1
        for _task, score in filtered_results:
            assert score >= 0.1

    def test_search_with_limit(self, db_with_tasks) -> None:
        """Test search with limit parameter."""
        db, manager, project_id = db_with_tasks

        results = manager.search_tasks(
            "user",
            project_id=project_id,
            limit=2,
        )

        assert len(results) <= 2

    def test_search_empty_query_returns_empty(self, db_with_tasks) -> None:
        """Test that empty query returns empty results."""
        db, manager, project_id = db_with_tasks

        results = manager.search_tasks("", project_id=project_id)
        assert len(results) == 0

    def test_search_results_include_scores(self, db_with_tasks) -> None:
        """Test that search results include similarity scores."""
        db, manager, project_id = db_with_tasks

        results = manager.search_tasks("authentication", project_id=project_id)

        for _task, score in results:
            assert isinstance(score, float)
            assert 0.0 <= score <= 1.0

    def test_search_results_sorted_by_score(self, db_with_tasks) -> None:
        """Test that results are sorted by score descending."""
        db, manager, project_id = db_with_tasks

        results = manager.search_tasks("user", project_id=project_id)

        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    def test_reindex_search(self, db_with_tasks) -> None:
        """Test reindex_search rebuilds the index."""
        db, manager, project_id = db_with_tasks

        # Perform initial search
        results1 = manager.search_tasks("authentication", project_id=project_id)

        # Reindex
        stats = manager.reindex_search(project_id)

        # Check stats
        assert "document_count" in stats
        assert stats["document_count"] > 0

        # Search again should work
        results2 = manager.search_tasks("authentication", project_id=project_id)
        assert len(results1) == len(results2)


class TestTaskFTS5Searcher:
    """Tests for the TaskFTS5Searcher class."""

    def test_fts5_searcher_search(self, db_with_tasks) -> None:
        """Test TaskFTS5Searcher direct search."""
        from gobby.storage.tasks._search import TaskFTS5Searcher

        db, manager, project_id = db_with_tasks

        searcher = TaskFTS5Searcher(db)
        results = searcher.search("authentication")

        assert len(results) > 0
        for task_id, score in results:
            assert isinstance(task_id, str)
            assert isinstance(score, float)
            assert 0.0 <= score <= 1.0

    def test_fts5_searcher_with_filters(self, db_with_tasks) -> None:
        """Test TaskFTS5Searcher with SQL filter push-down."""
        from gobby.storage.tasks._search import TaskFTS5Searcher

        db, manager, project_id = db_with_tasks

        searcher = TaskFTS5Searcher(db)

        # Search with project filter
        results = searcher.search("authentication", project_id=project_id)
        assert len(results) > 0

        # Search with non-matching project should return empty
        results = searcher.search("authentication", project_id="nonexistent")
        assert len(results) == 0

    def test_fts5_searcher_reindex(self, db_with_tasks) -> None:
        """Test FTS5 index rebuild."""
        from gobby.storage.tasks._search import TaskFTS5Searcher

        db, manager, project_id = db_with_tasks

        searcher = TaskFTS5Searcher(db)
        stats = searcher.reindex()

        assert stats["backend_type"] == "fts5"
        assert stats["document_count"] > 0

    def test_legacy_name_aliases_dialect_aware_searcher(self) -> None:
        """TaskFTS5Searcher remains as a compatibility alias."""
        from gobby.storage.tasks._search import TaskFTS5Searcher, TaskSearchBackend

        assert TaskFTS5Searcher is TaskSearchBackend

    def test_postgres_stage_state_search_uses_live_rows(self) -> None:
        """Postgres stage-state search normalizes fetched pg_search scores."""
        from gobby.storage.tasks._search import TaskSearchBackend

        class FakePostgresDB:
            dialect = "postgres"

            def __init__(self) -> None:
                self.sql = ""
                self.params: tuple[Any, ...] = ()

            def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
                self.sql = sql
                self.params = params
                return [{"id": "task-a", "score": 4.0}, {"id": "task-b", "score": 2.0}]

        db = FakePostgresDB()
        results = TaskSearchBackend(db).search(
            "alpha!!",
            top_k=2,
            current_stage_state=["ready", "in-progress"],
        )

        assert results == [("task-a", 1.0), ("task-b", 0.5)]
        assert "pdb.score(t.id)" in db.sql
        assert "(t.title @@@ $1 OR t.description @@@ $1)" in db.sql
        assert db.params == ("alpha", "ready", "in_progress", 2)

    def test_fts5_query_sanitization(self) -> None:
        """Test FTS5 query sanitization."""
        from gobby.search.fts5 import sanitize_fts_query

        # Normal query
        assert sanitize_fts_query("hello world") == '"hello" "world"'

        # Special characters stripped
        assert sanitize_fts_query("hello (world)") == '"hello" "world"'
        assert sanitize_fts_query('key:value "quoted"') == '"keyvalue" "quoted"'

        # Empty/whitespace
        assert sanitize_fts_query("") == ""
        assert sanitize_fts_query("   ") == ""

        # Underscores and hyphens preserved
        assert sanitize_fts_query("my_func") == '"my_func"'
        assert sanitize_fts_query("some-thing") == '"some-thing"'
