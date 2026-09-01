"""Tests for ToolMetricsStore."""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from gobby.mcp_proxy.metrics_store import ToolMetricsStore

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

PROJECT_1 = "88888888-8888-4888-8888-888888888881"
PROJECT_2 = "88888888-8888-4888-8888-888888888882"
PG_RECORD_CALL_PROJECT = "88888888-8888-4888-8888-888888888883"
PG_DAILY_AGGREGATE_PROJECT = "88888888-8888-4888-8888-888888888884"
OLD_METRICS_ID = "77777777-7777-4777-8777-777777777771"
PG_DAILY_METRICS_ID = "77777777-7777-4777-8777-777777777772"


@pytest.fixture
def metrics_store(temp_db: "HubDatabase") -> ToolMetricsStore:
    """Create a metrics store with temp database."""
    # Create test projects for foreign key constraints
    temp_db.execute(
        """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES (%s, %s, NOW(), NOW())
        """,
        (PROJECT_1, "Test Project 1"),
    )
    temp_db.execute(
        """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES (%s, %s, NOW(), NOW())
        """,
        (PROJECT_2, "Test Project 2"),
    )
    return ToolMetricsStore(temp_db)


def _insert_postgres_project(db: "HubDatabase", project_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    db.execute(
        """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES (%s, %s, %s, %s)
        """,
        (project_id, f"{project_id}-name", now, now),
    )


class TestToolMetricsStore:
    """Tests for ToolMetricsStore class."""

    def test_record_call(self, metrics_store: ToolMetricsStore) -> None:
        """Test recording a call in PostgreSQL."""
        metrics_store.record_call(
            server_name="test-server",
            tool_name="test_tool",
            project_id=PROJECT_1,
            latency_ms=100.0,
            success=True,
        )

        rows = metrics_store.get_metrics(project_id=PROJECT_1)
        assert len(rows) == 1
        assert rows[0]["call_count"] == 1
        assert rows[0]["success_count"] == 1
        assert rows[0]["failure_count"] == 0
        assert rows[0]["total_latency_ms"] == 100.0

    def test_record_multiple_calls(self, metrics_store: ToolMetricsStore) -> None:
        """Test multiple calls increment correctly."""
        for _ in range(3):
            metrics_store.record_call("s1", "t1", PROJECT_1, 100.0, True)
        for _ in range(2):
            metrics_store.record_call("s1", "t1", PROJECT_1, 200.0, False)

        rows = metrics_store.get_metrics(project_id=PROJECT_1)
        assert len(rows) == 1
        assert rows[0]["call_count"] == 5
        assert rows[0]["success_count"] == 3
        assert rows[0]["failure_count"] == 2
        assert rows[0]["total_latency_ms"] == 700.0  # 3*100 + 2*200

    def test_get_metrics_filters(self, metrics_store: ToolMetricsStore) -> None:
        """Test filtering metrics."""
        metrics_store.record_call("s1", "t1", PROJECT_1, 100.0)
        metrics_store.record_call("s2", "t2", PROJECT_2, 100.0)

        assert len(metrics_store.get_metrics(project_id=PROJECT_1)) == 1
        assert len(metrics_store.get_metrics(server_name="s1")) == 1
        assert len(metrics_store.get_metrics(tool_name="t2")) == 1

    def test_get_top_tools(self, metrics_store: ToolMetricsStore) -> None:
        """Test get_top_tools."""
        metrics_store.record_call("s1", "popular", PROJECT_1, 100.0)
        metrics_store.record_call("s1", "popular", PROJECT_1, 100.0)
        metrics_store.record_call("s1", "rare", PROJECT_1, 100.0)

        top = metrics_store.get_top_tools(limit=1)
        assert len(top) == 1
        assert top[0]["tool_name"] == "popular"

    def test_get_tool_success_rate(self, metrics_store: ToolMetricsStore) -> None:
        """Test get_tool_success_rate."""
        metrics_store.record_call("s1", "t1", PROJECT_1, 100.0, True)
        metrics_store.record_call("s1", "t1", PROJECT_1, 100.0, False)

        rate = metrics_store.get_tool_success_rate("s1", "t1", PROJECT_1)
        assert rate == 0.5

    def test_get_failing_tools(self, metrics_store: ToolMetricsStore) -> None:
        """Test get_failing_tools."""
        metrics_store.record_call("s1", "fail", PROJECT_1, 100.0, False)
        metrics_store.record_call("s1", "ok", PROJECT_1, 100.0, True)

        failing = metrics_store.get_failing_tools(threshold=0.5)
        assert len(failing) == 1
        assert failing[0]["tool_name"] == "fail"

    def test_reset_metrics(self, metrics_store: ToolMetricsStore) -> None:
        """Reset cascades across metric stores without affecting another project."""
        for project_id in (PROJECT_1, PROJECT_2):
            metrics_store.record_call("s1", "t1", project_id, 100.0)
            metrics_store.db.execute(
                """
                INSERT INTO tool_metrics_daily (
                    project_id, server_name, tool_name, date, call_count
                ) VALUES (%s, %s, %s, CURRENT_DATE, 1)
                """,
                (project_id, "s1", "t1"),
            )
            metrics_store.db.execute(
                """
                INSERT INTO metrics_events (event_type, project_id, server_name, name)
                VALUES ('tool_call', %s, %s, %s)
                """,
                (project_id, "s1", "t1"),
            )

        deleted = metrics_store.reset_metrics(
            project_id=PROJECT_1,
            server_name="s1",
            tool_name="t1",
        )

        assert deleted == 1
        for table, tool_column in (
            ("tool_metrics", "tool_name"),
            ("tool_metrics_daily", "tool_name"),
            ("metrics_events", "name"),
        ):
            deleted_project_count = metrics_store.db.fetchone(
                f"SELECT COUNT(*) AS count FROM {table} "  # nosec B608
                f"WHERE project_id = %s AND server_name = %s AND {tool_column} = %s",
                (PROJECT_1, "s1", "t1"),
            )
            preserved_project_count = metrics_store.db.fetchone(
                f"SELECT COUNT(*) AS count FROM {table} "  # nosec B608
                f"WHERE project_id = %s AND server_name = %s AND {tool_column} = %s",
                (PROJECT_2, "s1", "t1"),
            )
            assert deleted_project_count["count"] == 0
            assert preserved_project_count["count"] == 1

    def test_reset_metrics_rejects_unfiltered_delete(self, metrics_store: ToolMetricsStore) -> None:
        with pytest.raises(ValueError, match="at least one filter"):
            metrics_store.reset_metrics()

    def test_reset_metrics_preserves_non_tool_events(
        self,
        metrics_store: ToolMetricsStore,
    ) -> None:
        metrics_store.record_call("s1", "t1", PROJECT_1, 100.0)
        for event_type in ("tool_call", "rule_eval", "skill_search"):
            metrics_store.db.execute(
                """
                INSERT INTO metrics_events (event_type, project_id, server_name, name)
                VALUES (%s, %s, %s, %s)
                """,
                (event_type, PROJECT_1, "s1", "t1"),
            )

        deleted = metrics_store.reset_metrics(
            project_id=PROJECT_1,
            server_name="s1",
            tool_name="t1",
        )

        assert deleted == 1
        remaining = metrics_store.db.fetchall(
            """
            SELECT event_type FROM metrics_events
            WHERE project_id = %s AND server_name = %s AND name = %s
            ORDER BY event_type
            """,
            (PROJECT_1, "s1", "t1"),
        )
        assert [row["event_type"] for row in remaining] == ["rule_eval", "skill_search"]

    def test_reset_metrics_rolls_back_all_tables_on_failure(
        self,
        metrics_store: ToolMetricsStore,
        temp_db: "HubDatabase",
    ) -> None:
        metrics_store.record_call("s1", "t1", PROJECT_1, 100.0)
        temp_db.execute(
            """
            INSERT INTO tool_metrics_daily (
                project_id, server_name, tool_name, date, call_count
            ) VALUES (%s, %s, %s, CURRENT_DATE, 1)
            """,
            (PROJECT_1, "s1", "t1"),
        )
        temp_db.execute(
            """
            INSERT INTO metrics_events (event_type, project_id, server_name, name)
            VALUES ('tool_call', %s, %s, %s)
            """,
            (PROJECT_1, "s1", "t1"),
        )
        original_transaction = temp_db.transaction

        @contextmanager
        def interrupted_transaction() -> Iterator[Any]:
            with original_transaction() as txn:
                original_execute = txn.execute

                def execute_then_interrupt(
                    sql: str,
                    params: tuple[Any, ...] = (),
                ) -> Any:
                    cursor = original_execute(sql, params)
                    if "tool_metrics_daily" in sql:
                        raise RuntimeError("simulated reset interruption")
                    return cursor

                with patch.object(txn, "execute", side_effect=execute_then_interrupt):
                    yield txn

        with (
            patch.object(temp_db, "transaction", side_effect=interrupted_transaction),
            pytest.raises(RuntimeError, match="reset interruption"),
        ):
            metrics_store.reset_metrics(project_id=PROJECT_1)

        assert len(metrics_store.get_metrics(project_id=PROJECT_1)) == 1
        assert metrics_store.get_daily_metrics(project_id=PROJECT_1)[0]["call_count"] == 1
        event_count = temp_db.fetchone(
            "SELECT COUNT(*) AS count FROM metrics_events WHERE project_id = %s",
            (PROJECT_1,),
        )
        assert event_count["count"] == 1

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
            ) VALUES (%s, %s, %s, %s, 10, 8, 2, 1000.0, 100.0, %s, %s, %s)
            """,
            (OLD_METRICS_ID, PROJECT_1, "s1", "t1", old_time, old_time, old_time),
        )

        cutoff = datetime.now(UTC) - timedelta(days=7)
        deleted = metrics_store.cleanup_old_metrics(cutoff)
        assert deleted == 1

        daily = metrics_store.get_daily_metrics(project_id=PROJECT_1)
        assert len(daily) == 1
        assert daily[0]["call_count"] == 10

        assert len(metrics_store.get_metrics()) == 0

    def test_cleanup_rolls_back_if_commit_is_interrupted(
        self, metrics_store: ToolMetricsStore, temp_db: "HubDatabase"
    ) -> None:
        old_time = datetime.now(UTC) - timedelta(days=10)
        temp_db.execute(
            """
            INSERT INTO tool_metrics (
                id, project_id, server_name, tool_name,
                call_count, success_count, failure_count,
                total_latency_ms, avg_latency_ms,
                last_called_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, 10, 8, 2, 1000.0, 100.0, %s, %s, %s)
            """,
            (OLD_METRICS_ID, PROJECT_1, "s1", "t1", old_time, old_time, old_time),
        )
        original_transaction = temp_db.transaction

        @contextmanager
        def interrupted_transaction():
            with original_transaction() as txn:
                original_execute = txn.execute

                def execute_then_interrupt(sql, params=()):
                    original_execute(sql, params)
                    raise RuntimeError("simulated process interruption before commit")

                with patch.object(txn, "execute", side_effect=execute_then_interrupt):
                    yield txn

        cutoff = datetime.now(UTC) - timedelta(days=7)
        with (
            patch.object(temp_db, "transaction", side_effect=interrupted_transaction),
            pytest.raises(RuntimeError, match="process interruption"),
        ):
            metrics_store.cleanup_old_metrics(cutoff)

        assert len(metrics_store.get_metrics()) == 1
        assert metrics_store.get_daily_metrics(project_id=PROJECT_1) == []

    def test_cleanup_preserves_concurrent_writer(
        self, metrics_store: ToolMetricsStore, temp_db: "HubDatabase"
    ) -> None:
        old_time = datetime.now(UTC) - timedelta(days=10)
        temp_db.execute(
            """
            INSERT INTO tool_metrics (
                id, project_id, server_name, tool_name,
                call_count, success_count, failure_count,
                total_latency_ms, avg_latency_ms,
                last_called_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, 10, 8, 2, 1000.0, 100.0, %s, %s, %s)
            """,
            (OLD_METRICS_ID, PROJECT_1, "s1", "t1", old_time, old_time, old_time),
        )
        rollup_finished = threading.Event()
        release_commit = threading.Event()
        writer_transaction_open = threading.Event()
        original_transaction = temp_db.transaction

        @contextmanager
        def coordinated_transaction():
            with original_transaction() as txn:
                if threading.current_thread().name.startswith("metrics-cleanup"):
                    original_execute = txn.execute

                    def execute_and_signal(sql, params=()):
                        cursor = original_execute(sql, params)
                        rollup_finished.set()
                        return cursor

                    with patch.object(txn, "execute", side_effect=execute_and_signal):
                        yield txn
                    assert release_commit.wait(timeout=5)
                else:
                    writer_transaction_open.set()
                    yield txn

        cutoff = datetime.now(UTC) - timedelta(days=7)
        with (
            patch.object(temp_db, "transaction", side_effect=coordinated_transaction),
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="metrics-cleanup") as cleanup_pool,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="metrics-writer") as writer_pool,
        ):
            cleanup_future = cleanup_pool.submit(metrics_store.cleanup_old_metrics, cutoff)
            assert rollup_finished.wait(timeout=5)
            writer_future = writer_pool.submit(
                metrics_store.record_call,
                "s1",
                "t1",
                PROJECT_1,
                50.0,
                True,
            )
            assert writer_transaction_open.wait(timeout=5)
            release_commit.set()
            assert cleanup_future.result(timeout=5) == 1
            writer_future.result(timeout=5)

        daily = metrics_store.get_daily_metrics(project_id=PROJECT_1)
        assert daily[0]["call_count"] == 10
        current = metrics_store.get_metrics(project_id=PROJECT_1)
        assert len(current) == 1
        assert current[0]["call_count"] == 1


class TestPostgresToolMetricsStore:
    """PostgreSQL regressions for ON CONFLICT metric upserts."""

    pytestmark = pytest.mark.integration

    def test_record_call_upsert_merges_existing_row(self, postgres_db: "HubDatabase") -> None:
        project_id = PG_RECORD_CALL_PROJECT
        _insert_postgres_project(postgres_db, project_id)
        store = ToolMetricsStore(postgres_db)

        store.record_call("context7", "resolve-library-id", project_id, 10.0, True)
        store.record_call("context7", "resolve-library-id", project_id, 20.0, False)

        row = postgres_db.fetchone(
            """
            SELECT call_count, success_count, failure_count, total_latency_ms, avg_latency_ms
            FROM tool_metrics
            WHERE project_id = %s AND server_name = %s AND tool_name = %s
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
        project_id = PG_DAILY_AGGREGATE_PROJECT
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
            ) VALUES (%s, %s, %s, %s, 2, 1, 1, 300.0, 150.0, %s, %s, %s)
            """,
            (
                PG_DAILY_METRICS_ID,
                project_id,
                "context7",
                "get-docs",
                old_time,
                old_time,
                old_time,
            ),
        )
        postgres_db.execute(
            """
            INSERT INTO tool_metrics_daily (
                project_id, server_name, tool_name, date,
                call_count, success_count, failure_count,
                total_latency_ms, avg_latency_ms, created_at
            ) VALUES (%s, %s, %s, %s, 3, 2, 1, 300.0, 100.0, %s)
            """,
            (project_id, "context7", "get-docs", "2020-01-02", old_time),
        )

        assert store.aggregate_to_daily(retention_days=7) == 1

        row = postgres_db.fetchone(
            """
            SELECT call_count, success_count, failure_count, total_latency_ms, avg_latency_ms
            FROM tool_metrics_daily
            WHERE project_id = %s AND server_name = %s AND tool_name = %s AND date = %s
            """,
            (project_id, "context7", "get-docs", "2020-01-02"),
        )

        assert row is not None
        assert row["call_count"] == 5
        assert row["success_count"] == 3
        assert row["failure_count"] == 2
        assert row["total_latency_ms"] == 600.0
        assert row["avg_latency_ms"] == 120.0
