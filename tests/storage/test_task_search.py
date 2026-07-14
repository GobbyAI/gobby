"""Tests for task search functionality."""

from typing import Any

import psycopg
import pytest

from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from tests.storage.tasks._stage_test_helpers import set_stage_state

pytestmark = pytest.mark.unit


@pytest.fixture
def db_with_tasks(hub_db, tmp_path):
    """Create PostgreSQL-backed tasks for task-search testing."""
    project = LocalProjectManager(hub_db).create(
        name="task-search-test-project",
        repo_path=str(tmp_path),
    )
    project_id = project.id
    manager = LocalTaskManager(hub_db)

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

    return hub_db, manager, project_id


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
               SET closed_at = %s,
                   closed_reason = %s
             WHERE id = %s
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


class TestTaskSearchBackend:
    """Tests for the Postgres task search backend."""

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
        assert "(t.title @@@ %s OR t.description @@@ %s)" in db.sql
        assert db.params == ("alpha", "alpha", "ready", "in_progress", 2)

    def test_pg_search_query_sanitization(self) -> None:
        """Test pg_search query sanitization."""
        from gobby.search.keyword import sanitize_pg_search_query

        assert sanitize_pg_search_query("hello world") == "hello world"
        assert sanitize_pg_search_query("hello (world)") == "hello world"
        assert sanitize_pg_search_query('key:value "quoted"') == "key value quoted"
        assert sanitize_pg_search_query("func(arg) -> str") == "func arg str"
        assert sanitize_pg_search_query("-") == ""
        assert sanitize_pg_search_query("") == ""
        assert sanitize_pg_search_query("   ") == ""
        assert sanitize_pg_search_query("my_func") == "my_func"
        assert sanitize_pg_search_query("some-thing") == "some thing"
        assert sanitize_pg_search_query("alpha::beta -> list[str] &&") == ("alpha beta list str")
        assert sanitize_pg_search_query("!!! ---") == ""
        assert sanitize_pg_search_query("AND OR NOT") == "and or not"
        assert (
            sanitize_pg_search_query("salt AND pepper Or paprika nOt sugar")
            == "salt and pepper or paprika not sugar"
        )
        assert sanitize_pg_search_query("CANDY ORACLE NOTICE _NOT_") == (
            "CANDY ORACLE NOTICE _NOT_"
        )
        assert sanitize_pg_search_query('"salt AND pepper" OR "NOT"') == ("salt and pepper or not")

    def test_postgres_keyword_parse_error_raises_typed_error(self) -> None:
        """Known pg_search parse errors are exposed as query syntax failures."""
        from gobby.search.keyword import SearchQuerySyntaxError
        from gobby.storage.tasks._search import TaskSearchBackend

        class FakePostgresDB:
            dialect = "postgres"

            def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
                raise RuntimeError("could not parse query string: `content:(-)`")

        with pytest.raises(SearchQuerySyntaxError, match="plain words") as exc_info:
            TaskSearchBackend(FakePostgresDB()).search("alpha", top_k=5)

        assert exc_info.value.query == "alpha"
        assert isinstance(exc_info.value.__cause__, RuntimeError)

    def test_postgres_keyword_zero_rows_returns_empty(self) -> None:
        """A successful pg_search query with no rows is not a syntax failure."""
        from gobby.storage.tasks._search import TaskSearchBackend

        class FakePostgresDB:
            dialect = "postgres"

            def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
                return []

        assert TaskSearchBackend(FakePostgresDB()).search("alpha", top_k=5) == []

    def test_postgres_keyword_infrastructure_error_propagates(self) -> None:
        """Non-parser database failures remain visible to callers."""
        from gobby.storage.tasks._search import TaskSearchBackend

        class FakePostgresDB:
            dialect = "postgres"

            def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
                raise RuntimeError("database connection lost")

        with pytest.raises(RuntimeError, match="connection lost"):
            TaskSearchBackend(FakePostgresDB()).search("alpha", top_k=5)

    def test_memory_keyword_search_excludes_soft_deleted(self) -> None:
        """Memory BM25 search appends an explicit ``deleted_at IS NULL`` active clause.

        The shared ``filters`` mapping can only express column equality, so the
        soft-delete visibility gate has to be carried by the table's ``active_clause``;
        without it hidden rows leak into recall and underfill hydration (#17162).
        """
        from gobby.search.keyword import BM25SearchBackend

        class FakePostgresDB:
            dialect = "postgres"

            def __init__(self) -> None:
                self.sql = ""
                self.params: tuple[Any, ...] = ()

            def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
                self.sql = sql
                self.params = params
                return [{"id": "mem-a", "score": 3.0}]

        db = FakePostgresDB()
        results = BM25SearchBackend(db, "memories").search(
            "alpha", 5, filters={"project_id": "proj-1"}
        )

        assert [(hit.id, hit.score) for hit in results] == [("mem-a", 1.0)]
        assert "deleted_at IS NULL" in db.sql
        assert "(memories.project_id = %s OR memories.project_id IS NULL)" in db.sql
        # The active clause carries no bound parameter: two search-column terms, the
        # project_id filter, then the limit.
        assert db.params == ("alpha", "alpha", "proj-1", 5)

    def test_task_keyword_search_has_no_active_clause(self) -> None:
        """Tables without a soft-delete column (tasks) get no ``deleted_at`` clause."""
        from gobby.search.keyword import BM25SearchBackend

        class FakePostgresDB:
            dialect = "postgres"

            def __init__(self) -> None:
                self.sql = ""

            def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
                self.sql = sql
                return [{"id": "task-a", "score": 1.0}]

        db = FakePostgresDB()
        BM25SearchBackend(db, "tasks").search("alpha", 5)

        assert "deleted_at" not in db.sql

    def test_postgres_stage_state_parse_error_raises_typed_error(self) -> None:
        """Task stage-state search exposes the same typed syntax failure."""
        from gobby.search.keyword import SearchQuerySyntaxError
        from gobby.storage.tasks._search import TaskSearchBackend

        class FakePostgresDB:
            dialect = "postgres"

            def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
                raise psycopg.DatabaseError("could not parse query string: `title:(-)`")

        with pytest.raises(SearchQuerySyntaxError, match="plain words") as exc_info:
            TaskSearchBackend(FakePostgresDB()).search(
                "alpha",
                current_stage_state="ready",
            )

        assert exc_info.value.query == "alpha"
        assert isinstance(exc_info.value.__cause__, psycopg.DatabaseError)

    def test_postgres_stage_state_non_database_error_propagates(self) -> None:
        """Message matching alone does not classify unrelated exceptions as parser failures."""
        from gobby.storage.tasks._search import TaskSearchBackend

        class FakePostgresDB:
            dialect = "postgres"

            def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
                raise RuntimeError("could not parse query string: unrelated application failure")

        with pytest.raises(RuntimeError, match="unrelated application failure"):
            TaskSearchBackend(FakePostgresDB()).search(
                "alpha",
                current_stage_state="ready",
            )

    def test_postgres_stage_state_zero_rows_returns_empty(self) -> None:
        """A successful stage-state query with no rows remains an empty result."""
        from gobby.storage.tasks._search import TaskSearchBackend

        class FakePostgresDB:
            dialect = "postgres"

            def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
                return []

        results = TaskSearchBackend(FakePostgresDB()).search(
            "alpha",
            current_stage_state="ready",
        )

        assert results == []

    def test_postgres_stage_state_infrastructure_error_propagates(self) -> None:
        """Stage-state search does not convert infrastructure failures to no matches."""
        from gobby.storage.tasks._search import TaskSearchBackend

        class FakePostgresDB:
            dialect = "postgres"

            def fetchall(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
                raise RuntimeError("database connection lost")

        with pytest.raises(RuntimeError, match="connection lost"):
            TaskSearchBackend(FakePostgresDB()).search(
                "alpha",
                current_stage_state="ready",
            )
