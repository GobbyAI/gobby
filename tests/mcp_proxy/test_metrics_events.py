"""Tests for MetricsEventStore — event log, queries, and archiving."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from gobby.mcp_proxy.metrics_events import MetricsEventStore

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.database import LocalDatabase
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.fixture
def event_store(temp_db: "LocalDatabase") -> MetricsEventStore:
    return MetricsEventStore(temp_db)


class TestRecordEvent:
    def test_record_tool_call(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(
            event_type="tool_call",
            name="list_tools",
            project_id="proj-1",
            session_id="sess-1",
            server_name="gobby-tasks",
            success=True,
            latency_ms=42.5,
        )
        events = event_store.query_events(event_type="tool_call")
        assert len(events) == 1
        assert events[0]["name"] == "list_tools"
        assert events[0]["session_id"] == "sess-1"
        assert events[0]["server_name"] == "gobby-tasks"
        assert events[0]["success"] == 1
        assert events[0]["latency_ms"] == 42.5

    def test_record_rule_eval(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(
            event_type="rule_eval",
            name="task-before-edit",
            session_id="sess-1",
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
            session_id="sess-1",
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


class TestSessionToolBreakdown:
    def test_breakdown_groups_by_tool(self, event_store: MetricsEventStore) -> None:
        # Record multiple calls across tools
        for _ in range(5):
            event_store.record_event(
                event_type="tool_call",
                name="Read",
                session_id="sess-1",
                server_name="gobby-tasks",
                latency_ms=10.0,
            )
        for _ in range(3):
            event_store.record_event(
                event_type="tool_call",
                name="Edit",
                session_id="sess-1",
                server_name="gobby-tasks",
                latency_ms=20.0,
            )
        # Different session — should not appear
        event_store.record_event(
            event_type="tool_call",
            name="Read",
            session_id="sess-2",
            server_name="gobby-tasks",
            latency_ms=10.0,
        )

        breakdown = event_store.get_session_tool_breakdown("sess-1")
        assert len(breakdown) == 2
        # Sorted by call_count DESC
        assert breakdown[0]["tool_name"] == "Read"
        assert breakdown[0]["call_count"] == 5
        assert breakdown[1]["tool_name"] == "Edit"
        assert breakdown[1]["call_count"] == 3

    def test_empty_session(self, event_store: MetricsEventStore) -> None:
        breakdown = event_store.get_session_tool_breakdown("nonexistent")
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
        assert len(stats) == 2

        rule_a = next(s for s in stats if s["rule_name"] == "rule-a")
        assert rule_a["eval_count"] == 2
        assert rule_a["block_count"] == 1
        assert rule_a["allow_count"] == 1

    def test_filter_by_session(self, event_store: MetricsEventStore) -> None:
        event_store.record_event(
            event_type="rule_eval", name="rule-a", session_id="s1", result="allow"
        )
        event_store.record_event(
            event_type="rule_eval", name="rule-a", session_id="s2", result="block"
        )

        stats = event_store.get_rule_stats(session_id="s1")
        assert len(stats) == 1
        assert stats[0]["allow_count"] == 1


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
        self, event_store: MetricsEventStore, temp_db: "LocalDatabase"
    ) -> None:
        # Insert events with old timestamps
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        for i in range(5):
            temp_db.execute(
                """INSERT INTO metrics_events
                   (event_type, name, server_name, success, latency_ms, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("tool_call", "Read", "gobby-tasks", 1, 10.0 + i, old_date),
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

    def test_archive_upsert_merges(
        self, event_store: MetricsEventStore, temp_db: "LocalDatabase"
    ) -> None:
        """Running archive twice should merge counts, not duplicate rows."""
        old_date1 = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        old_date2 = (datetime.now(UTC) - timedelta(days=45)).isoformat()

        temp_db.execute(
            """INSERT INTO metrics_events
               (event_type, name, server_name, success, latency_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("tool_call", "Read", "gobby-tasks", 1, 10.0, old_date1),
        )
        event_store.archive_old_events(retention_days=30)

        temp_db.execute(
            """INSERT INTO metrics_events
               (event_type, name, server_name, success, latency_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("tool_call", "Read", "gobby-tasks", 1, 20.0, old_date2),
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


class TestPostgresArchive:
    """PostgreSQL regressions for ON CONFLICT archive upserts."""

    pytestmark = pytest.mark.integration

    def test_archive_old_events_upsert_merges_existing_row(
        self, postgres_db: "HubDatabase"
    ) -> None:
        event_store = MetricsEventStore(postgres_db)
        old_date = datetime(2020, 1, 2, 12, tzinfo=UTC).isoformat()
        project_id = "proj-pg-archive"

        postgres_db.execute(
            """
            INSERT INTO metrics_events_archive (
                event_type, project_id, server_name, name,
                call_count, success_count, failure_count,
                total_latency_ms, block_count, allow_count
            ) VALUES (?, ?, ?, ?, 2, 1, 1, 40.0, 1, 0)
            """,
            ("tool_call", project_id, "context7", "get-docs"),
        )
        postgres_db.execute(
            """
            INSERT INTO metrics_events (
                event_type, project_id, server_name, name,
                success, latency_ms, result, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("tool_call", project_id, "context7", "get-docs", True, 10.0, "allow", old_date),
        )
        postgres_db.execute(
            """
            INSERT INTO metrics_events (
                event_type, project_id, server_name, name,
                success, latency_ms, result, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            WHERE event_type = ? AND project_id = ? AND server_name = ? AND name = ?
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

    def test_record_call_writes_event(self, temp_db: "LocalDatabase") -> None:
        from gobby.mcp_proxy.metrics import ToolMetricsManager

        manager = ToolMetricsManager(temp_db)
        manager.record_call(
            server_name="gobby-tasks",
            tool_name="create_task",
            project_id="proj-1",
            latency_ms=50.0,
            success=True,
            session_id="sess-123",
        )

        # Check event was recorded
        events = manager.event_store.query_events(event_type="tool_call")
        assert len(events) == 1
        assert events[0]["name"] == "create_task"
        assert events[0]["session_id"] == "sess-123"
        assert events[0]["server_name"] == "gobby-tasks"

    def test_record_call_without_session_id(self, temp_db: "LocalDatabase") -> None:
        from gobby.mcp_proxy.metrics import ToolMetricsManager

        manager = ToolMetricsManager(temp_db)
        manager.record_call(
            server_name="gobby-tasks",
            tool_name="list_tools",
            project_id="proj-1",
            latency_ms=10.0,
        )

        events = manager.event_store.query_events(event_type="tool_call")
        assert len(events) == 1
        assert events[0]["session_id"] is None


class TestMCPTools:
    """Test the new MCP tool functions."""

    @pytest.fixture
    def registry(self, temp_db: "LocalDatabase") -> "InternalToolRegistry":
        from gobby.mcp_proxy.metrics import ToolMetricsManager
        from gobby.mcp_proxy.tools.metrics import create_metrics_registry

        manager = ToolMetricsManager(temp_db)
        return create_metrics_registry(
            metrics_manager=manager,
            event_store=manager.event_store,
        )

    @pytest.mark.asyncio
    async def test_get_session_tools(self, registry, temp_db: "LocalDatabase") -> None:
        event_store = MetricsEventStore(temp_db)
        event_store.record_event(
            event_type="tool_call",
            name="Read",
            session_id="s1",
            server_name="proxy",
            latency_ms=10.0,
        )
        event_store.record_event(
            event_type="tool_call",
            name="Read",
            session_id="s1",
            server_name="proxy",
            latency_ms=20.0,
        )

        result = await registry.call("get_session_tools", {"session_id": "s1"})
        assert result["success"] is True
        assert result["total_calls"] == 2
        assert len(result["tools"]) == 1

    @pytest.mark.asyncio
    async def test_get_rule_metrics(self, registry, temp_db: "LocalDatabase") -> None:
        event_store = MetricsEventStore(temp_db)
        event_store.record_event(event_type="rule_eval", name="task-rule", result="block")
        event_store.record_event(event_type="rule_eval", name="task-rule", result="allow")

        result = await registry.call("get_rule_metrics", {"hours": 1})
        assert result["success"] is True
        assert result["summary"]["total_evals"] == 2
        assert result["summary"]["total_blocks"] == 1

    @pytest.mark.asyncio
    async def test_get_skill_metrics(self, registry, temp_db: "LocalDatabase") -> None:
        event_store = MetricsEventStore(temp_db)
        event_store.record_event(event_type="skill_search", name="memory")
        event_store.record_event(event_type="skill_invoke", name="memory")

        result = await registry.call("get_skill_metrics", {"hours": 1})
        assert result["success"] is True
        assert result["summary"]["total_searches"] == 1
        assert result["summary"]["total_invocations"] == 1

    @pytest.mark.asyncio
    async def test_get_metrics_timeseries(self, registry, temp_db: "LocalDatabase") -> None:
        event_store = MetricsEventStore(temp_db)
        event_store.record_event(event_type="tool_call", name="Read", latency_ms=10.0)

        result = await registry.call(
            "get_metrics_timeseries", {"event_type": "tool_call", "time_range": "1h"}
        )
        assert result["success"] is True
        assert result["bucket_size"] == "minute"
        assert len(result["buckets"]) >= 1
