"""Tests for ToolMetricsStore."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from gobby.mcp_proxy.metrics_store import ToolMetricsStore

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.fixture
def metrics_store(temp_db: "HubDatabase") -> ToolMetricsStore:
    """Create a metrics store with temp database."""
    # Create test projects for foreign key constraints
    temp_db.execute(
        """
        INSERT INTO projects (id, name, repo_path, created_at, updated_at)
        VALUES (?, ?, ?, datetime('now'), datetime('now'))
        """,
        ("proj-1", "Test Project 1", "/tmp/test1"),
    )
    temp_db.execute(
        """
        INSERT INTO projects (id, name, repo_path, created_at, updated_at)
        VALUES (?, ?, ?, datetime('now'), datetime('now'))
        """,
        ("proj-2", "Test Project 2", "/tmp/test2"),
    )
    return ToolMetricsStore(temp_db)


def _insert_postgres_project(db: "HubDatabase", project_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    db.execute(
        """
        INSERT INTO projects (id, name, repo_path, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (project_id, f"{project_id}-name", f"/tmp/{project_id}", now, now),
    )


class TestToolMetricsStore:
    """Tests for ToolMetricsStore class."""

    def test_record_call(self, metrics_store: ToolMetricsStore) -> None:
        """Test recording a call in PostgreSQL."""
        metrics_store.record_call(
            server_name="test-server",
            tool_name="test_tool",
            project_id="proj-1",
            latency_ms=100.0,
            success=True,
        )

        rows = metrics_store.get_metrics(project_id="proj-1")
        assert len(rows) == 1
        assert rows[0]["call_count"] == 1
        assert rows[0]["success_count"] == 1
        assert rows[0]["failure_count"] == 0
        assert rows[0]["total_latency_ms"] == 100.0

    def test_record_multiple_calls(self, metrics_store: ToolMetricsStore) -> None:
        """Test multiple calls increment correctly."""
        for _ in range(3):
            metrics_store.record_call("s1", "t1", "proj-1", 100.0, True)
        for _ in range(2):
            metrics_store.record_call("s1", "t1", "proj-1", 200.0, False)

        rows = metrics_store.get_metrics(project_id="proj-1")
        assert len(rows) == 1
        assert rows[0]["call_count"] == 5
        assert rows[0]["success_count"] == 3
        assert rows[0]["failure_count"] == 2
        assert rows[0]["total_latency_ms"] == 700.0  # 3*100 + 2*200

    def test_get_metrics_filters(self, metrics_store: ToolMetricsStore) -> None:
        """Test filtering metrics."""
        metrics_store.record_call("s1", "t1", "proj-1", 100.0)
        metrics_store.record_call("s2", "t2", "proj-2", 100.0)

        assert len(metrics_store.get_metrics(project_id="proj-1")) == 1
        assert len(metrics_store.get_metrics(server_name="s1")) == 1
        assert len(metrics_store.get_metrics(tool_name="t2")) == 1

    def test_get_top_tools(self, metrics_store: ToolMetricsStore) -> None:
        """Test get_top_tools."""
        metrics_store.record_call("s1", "popular", "proj-1", 100.0)
        metrics_store.record_call("s1", "popular", "proj-1", 100.0)
        metrics_store.record_call("s1", "rare", "proj-1", 100.0)

        top = metrics_store.get_top_tools(limit=1)
        assert len(top) == 1
        assert top[0]["tool_name"] == "popular"

    def test_get_tool_success_rate(self, metrics_store: ToolMetricsStore) -> None:
        """Test get_tool_success_rate."""
        metrics_store.record_call("s1", "t1", "proj-1", 100.0, True)
        metrics_store.record_call("s1", "t1", "proj-1", 100.0, False)

        rate = metrics_store.get_tool_success_rate("s1", "t1", "proj-1")
        assert rate == 0.5

    def test_get_failing_tools(self, metrics_store: ToolMetricsStore) -> None:
        """Test get_failing_tools."""
        metrics_store.record_call("s1", "fail", "proj-1", 100.0, False)
        metrics_store.record_call("s1", "ok", "proj-1", 100.0, True)

        failing = metrics_store.get_failing_tools(threshold=0.5)
        assert len(failing) == 1
        assert failing[0]["tool_name"] == "fail"

    def test_reset_metrics(self, metrics_store: ToolMetricsStore) -> None:
        """Test resetting metrics."""
        metrics_store.record_call("s1", "t1", "proj-1", 100.0)
        deleted = metrics_store.reset_metrics(project_id="proj-1")
        assert deleted == 1
        assert len(metrics_store.get_metrics()) == 0

    def test_cleanup_and_aggregate(
        self, metrics_store: ToolMetricsStore, temp_db: "HubDatabase"
    ) -> None:
        """Test aggregation to daily and cleanup."""
        old_time = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        temp_db.execute(
            """
            INSERT INTO tool_metrics (
                id, project_id, server_name, tool_name,
                call_count, success_count, failure_count,
                total_latency_ms, avg_latency_ms,
                last_called_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 10, 8, 2, 1000.0, 100.0, ?, ?, ?)
            """,
            ("tm-old", "proj-1", "s1", "t1", old_time, old_time, old_time),
        )

        aggregated = metrics_store.aggregate_to_daily(retention_days=7)
        assert aggregated == 1

        daily = metrics_store.get_daily_metrics(project_id="proj-1")
        assert len(daily) == 1
        assert daily[0]["call_count"] == 10

        deleted = metrics_store.cleanup_old_metrics(retention_days=7)
        assert deleted == 1
        assert len(metrics_store.get_metrics()) == 0


class TestPostgresToolMetricsStore:
    """PostgreSQL regressions for ON CONFLICT metric upserts."""

    pytestmark = pytest.mark.integration

    def test_record_call_upsert_merges_existing_row(self, postgres_db: "HubDatabase") -> None:
        project_id = "proj-pg-record-call"
        _insert_postgres_project(postgres_db, project_id)
        store = ToolMetricsStore(postgres_db)

        store.record_call("context7", "resolve-library-id", project_id, 10.0, True)
        store.record_call("context7", "resolve-library-id", project_id, 20.0, False)

        row = postgres_db.fetchone(
            """
            SELECT call_count, success_count, failure_count, total_latency_ms, avg_latency_ms
            FROM tool_metrics
            WHERE project_id = ? AND server_name = ? AND tool_name = ?
            """,
            (project_id, "context7", "resolve-library-id"),
        )

        assert row is not None
        assert row["call_count"] == 2
        assert row["success_count"] == 1
        assert row["failure_count"] == 1
        assert row["total_latency_ms"] == 30.0
        assert row["avg_latency_ms"] == 15.0

    def test_aggregate_to_daily_upsert_merges_existing_row(
        self, postgres_db: "HubDatabase"
    ) -> None:
        project_id = "proj-pg-daily-aggregate"
        _insert_postgres_project(postgres_db, project_id)
        store = ToolMetricsStore(postgres_db)
        old_time = datetime(2020, 1, 2, 12, tzinfo=UTC).isoformat()

        postgres_db.execute(
            """
            INSERT INTO tool_metrics (
                id, project_id, server_name, tool_name,
                call_count, success_count, failure_count,
                total_latency_ms, avg_latency_ms,
                last_called_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 2, 1, 1, 300.0, 150.0, ?, ?, ?)
            """,
            ("tm-pg-daily", project_id, "context7", "get-docs", old_time, old_time, old_time),
        )
        postgres_db.execute(
            """
            INSERT INTO tool_metrics_daily (
                project_id, server_name, tool_name, date,
                call_count, success_count, failure_count,
                total_latency_ms, avg_latency_ms, created_at
            ) VALUES (?, ?, ?, ?, 3, 2, 1, 300.0, 100.0, ?)
            """,
            (project_id, "context7", "get-docs", "2020-01-02", old_time),
        )

        assert store.aggregate_to_daily(retention_days=7) == 1

        row = postgres_db.fetchone(
            """
            SELECT call_count, success_count, failure_count, total_latency_ms, avg_latency_ms
            FROM tool_metrics_daily
            WHERE project_id = ? AND server_name = ? AND tool_name = ? AND date = ?
            """,
            (project_id, "context7", "get-docs", "2020-01-02"),
        )

        assert row is not None
        assert row["call_count"] == 5
        assert row["success_count"] == 3
        assert row["failure_count"] == 2
        assert row["total_latency_ms"] == 600.0
        assert row["avg_latency_ms"] == 120.0
