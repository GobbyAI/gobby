"""Tests for MetricsEventStore — event log, queries, and archiving."""

import threading
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Never, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.mcp_proxy.metrics import ToolMetricsManager
from gobby.mcp_proxy.metrics_events import MetricsEventRecord, MetricsEventStore
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.mcp_proxy.tools.metrics import create_metrics_registry

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.storage.hub.protocol import Cursor, HubDatabase, Transaction

pytestmark = pytest.mark.unit

PROJECT_ID = "88888888-8888-4888-8888-888888888891"
PG_ARCHIVE_PROJECT_ID = "88888888-8888-4888-8888-888888888892"
SESSION_ID_1 = "99999999-9999-4999-9999-999999999991"
SESSION_ID_2 = "99999999-9999-4999-9999-999999999992"
SESSION_ID_3 = "99999999-9999-4999-9999-999999999993"
SESSION_ID_4 = "99999999-9999-4999-9999-999999999994"
NO_MATCH_SESSION_ID = "99999999-9999-4999-9999-999999999995"
FALLBACK_PROJECT_ID = "88888888-8888-4888-8888-888888888899"
SESSION_REF_1 = "#111"
SESSION_REF_2 = "#112"


def _create_metrics_proxy(
    temp_db: "HubDatabase",
) -> tuple[ToolProxyService, MagicMock, ToolMetricsManager]:
    metrics_manager = ToolMetricsManager(temp_db)
    mcp_manager = MagicMock()
    mcp_manager.project_id = FALLBACK_PROJECT_ID
    mcp_manager.metrics_manager = metrics_manager
    session_manager = MagicMock()
    session_ids = {
        SESSION_REF_1: SESSION_ID_1,
        SESSION_REF_2: SESSION_ID_2,
    }
    session_manager.resolve_session_reference.side_effect = (
        lambda session_ref, _project_id: session_ids.get(session_ref, session_ref)
    )
    session_manager.get.return_value = SimpleNamespace(project_id=PROJECT_ID)
    mcp_manager.session_manager = session_manager
    mcp_manager.has_server.side_effect = lambda server_name: server_name == "context7"

    internal_manager = InternalRegistryManager()
    memory_registry = InternalToolRegistry("gobby-memory")
    memory_registry.register(
        name="search_memories",
        description="Search memories.",
        input_schema={"type": "object", "properties": {}},
        func=lambda: {"matches": []},
    )
    tasks_registry = InternalToolRegistry("gobby-tasks")

    def fail_task_lookup() -> dict[str, Any]:
        raise RuntimeError("task lookup failed")

    tasks_registry.register(
        name="get_task",
        description="Get a task.",
        input_schema={"type": "object", "properties": {}},
        func=fail_task_lookup,
    )
    internal_manager.add_registry(memory_registry)
    internal_manager.add_registry(tasks_registry)
    internal_manager.add_registry(
        create_metrics_registry(
            metrics_manager,
            event_store=metrics_manager.event_store,
        )
    )
    hook_manager = cast("HookManager", SimpleNamespace(_session_manager=session_manager))

    proxy = ToolProxyService(
        mcp_manager=cast("MCPClientManager", mcp_manager),
        internal_manager=internal_manager,
        validate_arguments=False,
        hook_manager_resolver=lambda: hook_manager,
    )
    return proxy, mcp_manager, metrics_manager


@pytest.fixture
def event_store(temp_db: "HubDatabase") -> MetricsEventStore:
    return MetricsEventStore(temp_db)


class TestRecordEvent:
    def test_record_tool_call(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(
            event_type="tool_call",
            name="list_tools",
            project_id=PROJECT_ID,
            session_id=SESSION_ID_1,
            server_name="gobby-tasks",
            success=True,
            latency_ms=42.5,
        )
        events = event_store.query_events(event_type="tool_call")
        assert len(events) == 1
        assert events[0]["name"] == "list_tools"
        assert events[0]["session_id"] == SESSION_ID_1
        assert events[0]["server_name"] == "gobby-tasks"
        assert events[0]["success"] == 1
        assert events[0]["latency_ms"] == 42.5

    def test_record_rule_eval(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(
            event_type="rule_eval",
            name="task-before-edit",
            session_id=SESSION_ID_1,
            success=False,
            result="block",
            latency_ms=1.2,
        )
        events = event_store.query_events(event_type="rule_eval")
        assert len(events) == 1
        assert events[0]["result"] == "block"
        assert events[0]["success"] == 0

    def test_record_skill_search(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(
            event_type="skill_search",
            name="source-control",
            session_id=SESSION_ID_1,
            success=True,
            metadata={"query": "how to commit", "match_count": 3},
        )
        events = event_store.query_events(event_type="skill_search")
        assert len(events) == 1
        assert events[0]["name"] == "source-control"
        assert '"query"' in events[0]["metadata_json"]

    def test_record_failure(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(
            event_type="tool_call",
            name="broken_tool",
            success=False,
            latency_ms=100.0,
        )
        events = event_store.query_events()
        assert events[0]["success"] == 0

    def test_record_events_uses_one_batch_transaction(
        self,
        event_store: MetricsEventStore,
    ) -> None:
        records = [
            MetricsEventRecord(
                event_type="rule_eval",
                name="rule-one",
                session_id=SESSION_ID_1,
                latency_ms=1.5,
                result="allow",
            ),
            MetricsEventRecord(
                event_type="rule_eval",
                name="rule-two",
                session_id=SESSION_ID_1,
                success=False,
                latency_ms=2.5,
                result="block",
                metadata={"source": "test"},
            ),
        ]

        with patch.object(
            event_store.db,
            "executemany",
            wraps=event_store.db.executemany,
        ) as execute_many:
            event_store.record_events(records)

        execute_many.assert_called_once()
        events = event_store.query_events(
            event_type="rule_eval",
            session_id=SESSION_ID_1,
        )
        assert {event["name"] for event in events} == {"rule-one", "rule-two"}
        blocked = next(event for event in events if event["name"] == "rule-two")
        assert blocked["success"] == 0
        assert blocked["result"] == "block"

    def test_record_events_empty_batch_does_not_write(
        self,
        event_store: MetricsEventStore,
    ) -> None:
        events_before = event_store.query_events()
        with patch.object(event_store.db, "executemany") as execute_many:
            event_store.record_events([])

        execute_many.assert_not_called()
        assert event_store.query_events() == events_before


class TestSessionToolBreakdown:
    def test_breakdown_groups_by_tool(self, event_store: MetricsEventStore) -> None:
        # Record multiple calls across tools
        for _ in range(5):
            event_store.record_event(
                event_type="tool_call",
                name="Read",
                session_id=SESSION_ID_1,
                server_name="gobby-tasks",
                latency_ms=10.0,
            )
        for _ in range(3):
            event_store.record_event(
                event_type="tool_call",
                name="Edit",
                session_id=SESSION_ID_1,
                server_name="gobby-tasks",
                latency_ms=20.0,
            )
        # Different session — should not appear
        event_store.record_event(
            event_type="tool_call",
            name="Read",
            session_id=SESSION_ID_2,
            server_name="gobby-tasks",
            latency_ms=10.0,
        )

        breakdown = event_store.get_session_tool_breakdown(SESSION_ID_1)
        assert len(breakdown) == 2
        # Sorted by call_count DESC
        assert breakdown[0]["tool_name"] == "Read"
        assert breakdown[0]["call_count"] == 5
        assert breakdown[1]["tool_name"] == "Edit"
        assert breakdown[1]["call_count"] == 3

    def test_empty_session(self, event_store: MetricsEventStore) -> None:
        breakdown = event_store.get_session_tool_breakdown(NO_MATCH_SESSION_ID)
        assert breakdown == []


class TestRuleStats:
    def test_aggregate_by_rule(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(
            event_type="rule_eval", name="rule-a", result="allow", latency_ms=1.0
        )
        event_store.record_event(
            event_type="rule_eval", name="rule-a", result="block", latency_ms=2.0
        )
        event_store.record_event(
            event_type="rule_eval", name="rule-b", result="allow", latency_ms=0.5
        )

        stats = event_store.get_rule_stats()
        assert stats == [
            {
                "rule_name": "rule-a",
                "block_count": 1,
                "avg_latency_ms": 2.0,
            }
        ]

    def test_filter_by_session(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(
            event_type="rule_eval",
            name="rule-a",
            session_id=SESSION_ID_3,
            result="allow",
        )
        event_store.record_event(
            event_type="rule_eval",
            name="rule-a",
            session_id=SESSION_ID_2,
            result="block",
        )

        assert event_store.get_rule_stats(session_id=SESSION_ID_3) == []
        assert event_store.get_rule_stats(session_id=SESSION_ID_2) == [
            {
                "rule_name": "rule-a",
                "block_count": 1,
                "avg_latency_ms": None,
            }
        ]

    def test_rule_timeseries_excludes_historical_allows(
        self, event_store: MetricsEventStore
    ) -> None:
        event_store.record_event(event_type="rule_eval", name="rule-a", result="allow")
        event_store.record_event(event_type="rule_eval", name="rule-a", result="block")

        result = event_store.get_timeseries("rule_eval", range_key="24h")

        assert sum(bucket["call_count"] for bucket in result["buckets"]) == 1
        assert all("allow_count" not in bucket for bucket in result["buckets"])


class TestSkillStats:
    def test_aggregate_skill_events(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(event_type="skill_search", name="memory")
        event_store.record_event(event_type="skill_search", name="memory")
        event_store.record_event(event_type="skill_invoke", name="memory")

        stats = event_store.get_skill_stats()
        assert len(stats) == 2  # two event types
        searches = next(s for s in stats if s["event_type"] == "skill_search")
        assert searches["count"] == 2
        invokes = next(s for s in stats if s["event_type"] == "skill_invoke")
        assert invokes["count"] == 1


class TestTimeseries:
    def test_24h_range(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(event_type="tool_call", name="Read", latency_ms=10.0)
        event_store.record_event(event_type="tool_call", name="Read", latency_ms=20.0)

        result = event_store.get_timeseries("tool_call", range_key="24h")
        assert result["range"] == "24h"
        assert result["bucket_size"] == "hour"
        assert len(result["buckets"]) >= 1
        assert result["buckets"][0]["call_count"] == 2

    def test_all_range_includes_archive(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(event_type="tool_call", name="Read")

        result = event_store.get_timeseries("tool_call", range_key="all")
        assert "archive_totals" in result

    def test_1h_range_uses_minute_buckets(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(event_type="tool_call", name="Read")
        result = event_store.get_timeseries("tool_call", range_key="1h")
        assert result["bucket_size"] == "minute"

    def test_7d_range_uses_day_buckets(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(event_type="tool_call", name="Read")
        result = event_store.get_timeseries("tool_call", range_key="7d")
        assert result["bucket_size"] == "day"


class TestQueryEvents:
    def test_filter_by_type(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(event_type="tool_call", name="Read")
        event_store.record_event(event_type="rule_eval", name="rule-a")

        tools = event_store.query_events(event_type="tool_call")
        assert len(tools) == 1
        assert tools[0]["event_type"] == "tool_call"

    def test_filter_by_name(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(event_type="tool_call", name="Read")
        event_store.record_event(event_type="tool_call", name="Edit")

        results = event_store.query_events(name="Read")
        assert len(results) == 1

    def test_limit(self, event_store: MetricsEventStore) -> None:
        for i in range(10):
            event_store.record_event(event_type="tool_call", name=f"tool-{i}")

        results = event_store.query_events(limit=3)
        assert len(results) == 3

    def test_filter_by_since(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(event_type="tool_call", name="Read")
        # Query for events in the future — should return nothing
        future = datetime.now(UTC) + timedelta(hours=1)
        results = event_store.query_events(since=future)
        assert len(results) == 0


class TestArchive:
    def test_archive_old_events(
        self, event_store: MetricsEventStore, temp_db: "HubDatabase"
    ) -> None:
        # Insert events with old timestamps
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        for i in range(5):
            temp_db.execute(
                """INSERT INTO metrics_events
                   (event_type, name, server_name, success, latency_ms, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                ("tool_call", "Read", "gobby-tasks", True, 10.0 + i, old_date),
            )
        # Insert a recent event that should survive
        event_store.record_event(event_type="tool_call", name="Edit", latency_ms=5.0)

        archived = event_store.archive_old_events(retention_days=30)
        assert archived == 5

        # Check archive has aggregated data
        totals = event_store.get_archive_totals(event_type="tool_call")
        assert len(totals) == 1
        assert totals[0]["name"] == "Read"
        assert totals[0]["call_count"] == 5
        assert totals[0]["success_count"] == 5

        # Recent event still in main table
        remaining = event_store.query_events(event_type="tool_call")
        assert len(remaining) == 1
        assert remaining[0]["name"] == "Edit"

    def test_rule_eval_archive_totals_have_block_shape(
        self, event_store: MetricsEventStore, temp_db: "HubDatabase"
    ) -> None:
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        for result in ("block", "block", "allow"):
            temp_db.execute(
                """INSERT INTO metrics_events
                   (event_type, project_id, server_name, name, success, result, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    "rule_eval",
                    PROJECT_ID,
                    "rules",
                    "task-before-edit",
                    result == "allow",
                    result,
                    old_date,
                ),
            )

        assert event_store.archive_old_events(retention_days=30) == 3
        assert event_store.get_archive_totals(event_type="rule_eval") == [
            {
                "event_type": "rule_eval",
                "project_id": PROJECT_ID,
                "server_name": "rules",
                "name": "task-before-edit",
                "block_count": 2,
            }
        ]

    def test_archive_upsert_merges(
        self, event_store: MetricsEventStore, temp_db: "HubDatabase"
    ) -> None:
        """Running archive twice should merge counts, not duplicate rows."""
        old_date1 = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        old_date2 = (datetime.now(UTC) - timedelta(days=45)).isoformat()

        temp_db.execute(
            """INSERT INTO metrics_events
               (event_type, name, server_name, success, latency_ms, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            ("tool_call", "Read", "gobby-tasks", True, 10.0, old_date1),
        )
        event_store.archive_old_events(retention_days=30)

        temp_db.execute(
            """INSERT INTO metrics_events
               (event_type, name, server_name, success, latency_ms, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            ("tool_call", "Read", "gobby-tasks", True, 20.0, old_date2),
        )
        event_store.archive_old_events(retention_days=30)

        totals = event_store.get_archive_totals(event_type="tool_call")
        assert len(totals) == 1
        assert totals[0]["call_count"] == 2
        assert totals[0]["total_latency_ms"] == 30.0

    def test_archive_no_old_events(self, event_store: MetricsEventStore) -> None:
        """Archive with no old events should return 0."""
        event_store.record_event(event_type="tool_call", name="Read")
        assert event_store.archive_old_events(retention_days=30) == 0

    def test_archive_rolls_back_if_commit_is_interrupted(
        self, event_store: MetricsEventStore, temp_db: "HubDatabase"
    ) -> None:
        old_date = datetime.now(UTC) - timedelta(days=60)
        temp_db.execute(
            """INSERT INTO metrics_events
               (event_type, name, server_name, success, latency_ms, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            ("tool_call", "Read", "gobby-tasks", True, 10.0, old_date),
        )
        original_transaction = temp_db.transaction

        @contextmanager
        def interrupted_transaction() -> Iterator["Transaction"]:
            with original_transaction() as txn:
                original_execute = txn.execute

                def execute_then_interrupt(
                    sql: str,
                    params: Sequence[Any] | Mapping[str, Any] = (),
                ) -> Never:
                    original_execute(sql, params)
                    raise RuntimeError("simulated process interruption before commit")

                with patch.object(txn, "execute", side_effect=execute_then_interrupt):
                    yield txn

        with (
            patch.object(temp_db, "transaction", side_effect=interrupted_transaction),
            pytest.raises(RuntimeError, match="process interruption"),
        ):
            event_store.archive_old_events(retention_days=30)

        assert len(event_store.query_events(event_type="tool_call")) == 1
        assert event_store.get_archive_totals(event_type="tool_call") == []

    def test_archive_preserves_concurrent_old_event(
        self, event_store: MetricsEventStore, temp_db: "HubDatabase"
    ) -> None:
        old_date = datetime.now(UTC) - timedelta(days=60)
        temp_db.execute(
            """INSERT INTO metrics_events
               (event_type, name, server_name, success, latency_ms, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            ("tool_call", "first", "gobby-tasks", True, 10.0, old_date),
        )
        rollup_finished = threading.Event()
        release_commit = threading.Event()
        writer_transaction_open = threading.Event()
        original_transaction = temp_db.transaction

        @contextmanager
        def coordinated_transaction() -> Iterator["Transaction"]:
            with original_transaction() as txn:
                if threading.current_thread().name.startswith("archive-job"):
                    original_execute = txn.execute

                    def execute_and_signal(
                        sql: str,
                        params: Sequence[Any] | Mapping[str, Any] = (),
                    ) -> "Cursor":
                        cursor = original_execute(sql, params)
                        rollup_finished.set()
                        return cursor

                    with patch.object(txn, "execute", side_effect=execute_and_signal):
                        yield txn
                    assert release_commit.wait(timeout=5)
                else:
                    writer_transaction_open.set()
                    yield txn

        def insert_concurrent_event() -> None:
            temp_db.execute(
                """INSERT INTO metrics_events
                   (event_type, name, server_name, success, latency_ms, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                ("tool_call", "concurrent", "gobby-tasks", True, 20.0, old_date),
            )

        with (
            patch.object(temp_db, "transaction", side_effect=coordinated_transaction),
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="archive-job") as archive_pool,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="event-writer") as writer_pool,
        ):
            archive_future = archive_pool.submit(event_store.archive_old_events, 30)
            assert rollup_finished.wait(timeout=5)
            writer_future = writer_pool.submit(insert_concurrent_event)
            assert writer_transaction_open.wait(timeout=5)
            writer_future.result(timeout=5)
            release_commit.set()
            assert archive_future.result(timeout=5) == 1

        totals = event_store.get_archive_totals(event_type="tool_call")
        assert totals[0]["call_count"] == 1
        remaining = event_store.query_events(event_type="tool_call")
        assert [row["name"] for row in remaining] == ["concurrent"]


class TestPostgresArchive:
    """PostgreSQL regressions for ON CONFLICT archive upserts."""

    pytestmark = pytest.mark.integration

    def test_archive_old_events_upsert_merges_existing_row(
        self, postgres_db: "HubDatabase"
    ) -> None:
        event_store = MetricsEventStore(postgres_db)
        old_date = datetime(2020, 1, 2, 12, tzinfo=UTC).isoformat()
        project_id = PG_ARCHIVE_PROJECT_ID

        postgres_db.execute(
            """
            INSERT INTO metrics_events_archive (
                event_type, project_id, server_name, name,
                call_count, success_count, failure_count,
                total_latency_ms, block_count, allow_count
            ) VALUES (%s, %s, %s, %s, 2, 1, 1, 40.0, 1, 0)
            """,
            ("tool_call", project_id, "context7", "get-docs"),
        )
        postgres_db.execute(
            """
            INSERT INTO metrics_events (
                event_type, project_id, server_name, name,
                success, latency_ms, result, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            ("tool_call", project_id, "context7", "get-docs", True, 10.0, "allow", old_date),
        )
        postgres_db.execute(
            """
            INSERT INTO metrics_events (
                event_type, project_id, server_name, name,
                success, latency_ms, result, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            ("tool_call", project_id, "context7", "get-docs", False, 20.0, "block", old_date),
        )

        assert event_store.archive_old_events(retention_days=30) == 2

        row = postgres_db.fetchone(
            """
            SELECT
                call_count, success_count, failure_count,
                total_latency_ms, block_count, allow_count
            FROM metrics_events_archive
            WHERE event_type = %s AND project_id = %s AND server_name = %s AND name = %s
            """,
            ("tool_call", project_id, "context7", "get-docs"),
        )

        assert row is not None
        assert row["call_count"] == 4
        assert row["success_count"] == 2
        assert row["failure_count"] == 2
        assert row["total_latency_ms"] == 70.0
        assert row["block_count"] == 2
        assert row["allow_count"] == 1


class TestMetricsManagerIntegration:
    """Test that ToolMetricsManager dual-writes to event store."""

    def test_record_call_writes_event(self, temp_db: "HubDatabase") -> None:
        from gobby.mcp_proxy.metrics import ToolMetricsManager

        manager = ToolMetricsManager(temp_db)
        manager.record_call(
            server_name="gobby-tasks",
            tool_name="create_task",
            project_id=PROJECT_ID,
            latency_ms=50.0,
            success=True,
            session_id=SESSION_ID_4,
        )

        # Check event was recorded
        events = manager.event_store.query_events(event_type="tool_call")
        assert len(events) == 1
        assert events[0]["name"] == "create_task"
        assert events[0]["session_id"] == SESSION_ID_4
        assert events[0]["server_name"] == "gobby-tasks"

    def test_record_call_without_session_id(self, temp_db: "HubDatabase") -> None:
        from gobby.mcp_proxy.metrics import ToolMetricsManager

        manager = ToolMetricsManager(temp_db)
        manager.record_call(
            server_name="gobby-tasks",
            tool_name="list_tools",
            project_id=PROJECT_ID,
            latency_ms=10.0,
        )

        events = manager.event_store.query_events(event_type="tool_call")
        assert len(events) == 1
        assert events[0]["session_id"] is None


class TestInternalToolMetricsIntegration:
    @pytest.mark.asyncio
    async def test_internal_calls_record_effective_context_and_outcomes(
        self,
        temp_db: "HubDatabase",
    ) -> None:
        proxy, _, metrics_manager = _create_metrics_proxy(temp_db)

        memory_result = await proxy.call_tool(
            "gobby-memory",
            "search_memories",
            session_id=SESSION_REF_1,
        )
        task_result = await proxy.call_tool(
            "gobby-tasks",
            "get_task",
            session_id=SESSION_REF_1,
        )

        assert memory_result == {"matches": []}
        assert task_result["success"] is False
        assert task_result["error"] == "task lookup failed"

        summary = await proxy.call_tool(
            "gobby-metrics",
            "get_session_tools",
            {"session_id": SESSION_REF_1},
            session_id=SESSION_REF_1,
        )
        tools = {(row["server_name"], row["tool_name"]): row for row in summary["tools"]}

        assert summary["total_calls"] == 2
        assert tools[("gobby-memory", "search_memories")]["success_count"] == 1
        assert tools[("gobby-memory", "search_memories")]["failure_count"] == 0
        assert tools[("gobby-tasks", "get_task")]["success_count"] == 0
        assert tools[("gobby-tasks", "get_task")]["failure_count"] == 1

        events = metrics_manager.event_store.query_events(
            event_type="tool_call",
            session_id=SESSION_ID_1,
        )
        subject_events = [event for event in events if event["server_name"] != "gobby-metrics"]
        assert {event["project_id"] for event in subject_events} == {PROJECT_ID}
        assert all(event["latency_ms"] >= 0 for event in subject_events)

    @pytest.mark.asyncio
    async def test_mixed_internal_and_external_calls_aggregate_once_each(
        self,
        temp_db: "HubDatabase",
    ) -> None:
        proxy, mcp_manager, metrics_manager = _create_metrics_proxy(temp_db)

        async def call_external(
            server_name: str,
            tool_name: str,
            arguments: dict[str, Any],
            *,
            session_id: str,
        ) -> dict[str, Any]:
            metrics_manager.record_call(
                server_name=server_name,
                tool_name=tool_name,
                project_id=PROJECT_ID,
                latency_ms=5.0,
                session_id=session_id,
            )
            return {"content": [{"type": "text", "text": "ok"}]}

        mcp_manager.call_tool = AsyncMock(side_effect=call_external)

        await proxy.call_tool(
            "context7",
            "resolve-library-id",
            {"libraryName": "pytest"},
            session_id=SESSION_REF_2,
        )
        await proxy.call_tool(
            "gobby-memory",
            "search_memories",
            session_id=SESSION_REF_2,
        )

        summary = await proxy.call_tool(
            "gobby-metrics",
            "get_session_tools",
            {"session_id": SESSION_REF_2},
            session_id=SESSION_REF_2,
        )
        counts = {
            (row["server_name"], row["tool_name"]): row["call_count"] for row in summary["tools"]
        }

        assert summary["total_calls"] == 2
        assert counts == {
            ("context7", "resolve-library-id"): 1,
            ("gobby-memory", "search_memories"): 1,
        }


class TestMCPTools:
    """Test the new MCP tool functions."""

    @pytest.fixture
    def registry(self, temp_db: "HubDatabase") -> "InternalToolRegistry":
        from gobby.mcp_proxy.metrics import ToolMetricsManager
        from gobby.mcp_proxy.tools.metrics import create_metrics_registry

        manager = ToolMetricsManager(temp_db)
        return create_metrics_registry(
            metrics_manager=manager,
            event_store=manager.event_store,
        )

    @pytest.mark.asyncio
    async def test_reset_metrics_requires_project_context(self) -> None:
        from gobby.mcp_proxy.tools.metrics import create_metrics_registry

        manager = MagicMock()
        registry = create_metrics_registry(metrics_manager=manager)

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await registry.call("reset_metrics", {})

        assert result["success"] is False
        assert "project context" in result["error"]
        manager.reset_metrics.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_metrics_tools_force_calling_project_scope(self) -> None:
        from gobby.mcp_proxy.tools.metrics import create_metrics_registry

        manager = MagicMock()
        manager.reset_metrics.side_effect = [3, 2]
        registry = create_metrics_registry(metrics_manager=manager)
        reset_schema = registry._tools["reset_metrics"].input_schema

        assert set(reset_schema["properties"]) == {"server_name", "tool_name"}

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "calling-project"},
        ):
            reset_result = await registry.call("reset_metrics", {"server_name": "server-a"})
            tool_result = await registry.call(
                "reset_tool_metrics",
                {"server_name": "server-a", "tool_name": "tool-a"},
            )
            with pytest.raises(
                ValueError,
                match="Unknown argument\\(s\\) for tool 'reset_metrics': project_id",
            ):
                await registry.call("reset_metrics", {"project_id": "other-project"})

        assert reset_result["success"] is True
        assert tool_result["success"] is True
        assert manager.reset_metrics.call_args_list == [
            call(project_id="calling-project", server_name="server-a", tool_name=None),
            call(project_id="calling-project", server_name="server-a", tool_name="tool-a"),
        ]

    @pytest.mark.asyncio
    async def test_get_session_tools(
        self,
        registry: "InternalToolRegistry",
        temp_db: "HubDatabase",
    ) -> None:
        event_store = MetricsEventStore(temp_db)
        event_store.record_event(
            event_type="tool_call",
            name="Read",
            session_id=SESSION_ID_3,
            server_name="proxy",
            latency_ms=10.0,
        )
        event_store.record_event(
            event_type="tool_call",
            name="Read",
            session_id=SESSION_ID_3,
            server_name="proxy",
            latency_ms=20.0,
        )

        result = await registry.call("get_session_tools", {"session_id": SESSION_ID_3})
        assert result["success"] is True
        assert result["total_calls"] == 2
        assert len(result["tools"]) == 1

    @pytest.mark.asyncio
    async def test_get_rule_metrics(
        self,
        registry: "InternalToolRegistry",
        temp_db: "HubDatabase",
    ) -> None:
        event_store = MetricsEventStore(temp_db)
        event_store.record_event(event_type="rule_eval", name="task-rule", result="block")
        event_store.record_event(event_type="rule_eval", name="task-rule", result="allow")

        result = await registry.call("get_rule_metrics", {"hours": 1})
        assert result["success"] is True
        assert result["summary"]["total_blocks"] == 1
        assert "total_evals" not in result["summary"]
        assert "total_allows" not in result["summary"]

    @pytest.mark.asyncio
    async def test_get_skill_metrics(
        self,
        registry: "InternalToolRegistry",
        temp_db: "HubDatabase",
    ) -> None:
        event_store = MetricsEventStore(temp_db)
        event_store.record_event(event_type="skill_search", name="memory")
        event_store.record_event(event_type="skill_invoke", name="memory")

        result = await registry.call("get_skill_metrics", {"hours": 1})
        assert result["success"] is True
        assert result["summary"]["total_searches"] == 1
        assert result["summary"]["total_invocations"] == 1

    @pytest.mark.asyncio
    async def test_get_metrics_timeseries(
        self,
        registry: "InternalToolRegistry",
        temp_db: "HubDatabase",
    ) -> None:
        event_store = MetricsEventStore(temp_db)
        event_store.record_event(event_type="tool_call", name="Read", latency_ms=10.0)

        result = await registry.call(
            "get_metrics_timeseries", {"event_type": "tool_call", "time_range": "1h"}
        )
        assert result["success"] is True
        assert result["bucket_size"] == "minute"
        assert len(result["buckets"]) >= 1
