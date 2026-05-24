"""Tests for ToolMetricsManager."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.metrics import ToolMetrics, ToolMetricsManager

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_telemetry():
    """Mock telemetry metrics."""
    with patch("gobby.mcp_proxy.metrics.get_telemetry_metrics") as mock_get:
        mock_metrics = MagicMock()
        mock_get.return_value = mock_metrics
        yield mock_metrics


@pytest.fixture
def metrics_manager(temp_db: "HubDatabase", mock_telemetry) -> ToolMetricsManager:
    """Create a metrics manager with temp database."""
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
    return ToolMetricsManager(temp_db)


class TestToolMetrics:
    """Tests for ToolMetrics dataclass."""

    def test_from_row(self) -> None:
        """Test creating ToolMetrics from database row."""
        row = {
            "id": "tm-123",
            "project_id": "proj-1",
            "server_name": "test-server",
            "tool_name": "test_tool",
            "call_count": 10,
            "success_count": 8,
            "failure_count": 2,
            "total_latency_ms": 1000.0,
            "avg_latency_ms": 100.0,
            "last_called_at": "2024-01-01T12:00:00",
            "created_at": "2024-01-01T10:00:00",
            "updated_at": "2024-01-01T12:00:00",
        }
        metrics = ToolMetrics.from_row(row)

        assert metrics.id == "tm-123"
        assert metrics.project_id == "proj-1"
        assert metrics.server_name == "test-server"
        assert metrics.tool_name == "test_tool"
        assert metrics.call_count == 10
        assert metrics.success_count == 8
        assert metrics.failure_count == 2
        assert metrics.total_latency_ms == 1000.0
        assert metrics.avg_latency_ms == 100.0

    def test_to_dict(self) -> None:
        """Test converting ToolMetrics to dictionary."""
        metrics = ToolMetrics(
            id="tm-123",
            project_id="proj-1",
            server_name="test-server",
            tool_name="test_tool",
            call_count=10,
            success_count=8,
            failure_count=2,
            total_latency_ms=1000.0,
            avg_latency_ms=100.0,
            last_called_at="2024-01-01T12:00:00",
            created_at="2024-01-01T10:00:00",
            updated_at="2024-01-01T12:00:00",
        )
        result = metrics.to_dict()

        assert result["id"] == "tm-123"
        assert result["project_id"] == "proj-1"
        assert result["server_name"] == "test-server"
        assert result["tool_name"] == "test_tool"
        assert result["call_count"] == 10
        assert result["success_count"] == 8
        assert result["failure_count"] == 2
        assert result["success_rate"] == 0.8

    def test_to_dict_success_rate_zero_calls(self) -> None:
        """Test success_rate is None when call_count is 0."""
        metrics = ToolMetrics(
            id="tm-123",
            project_id="proj-1",
            server_name="test-server",
            tool_name="test_tool",
            call_count=0,
            success_count=0,
            failure_count=0,
            total_latency_ms=0.0,
            avg_latency_ms=None,
            last_called_at=None,
            created_at="2024-01-01T10:00:00",
            updated_at="2024-01-01T10:00:00",
        )
        result = metrics.to_dict()
        assert result["success_rate"] is None


class TestRecordCall:
    """Tests for record_call method."""

    def test_record_first_call_success(
        self, metrics_manager: ToolMetricsManager, mock_telemetry
    ) -> None:
        """Test recording first successful call creates new record."""
        metrics_manager.record_call(
            server_name="test-server",
            tool_name="test_tool",
            project_id="proj-1",
            latency_ms=100.0,
            success=True,
        )

        result = metrics_manager.get_metrics(project_id="proj-1")
        assert result["summary"]["total_calls"] == 1
        assert result["summary"]["total_success"] == 1
        assert result["summary"]["total_failure"] == 0

        # Verify OTel calls
        mock_telemetry.inc_counter.assert_any_call(
            "mcp_tool_calls_total",
            attributes={
                "server_name": "test-server",
                "tool_name": "test_tool",
                "success": "true",
                "project_id": "proj-1",
            },
        )
        mock_telemetry.inc_counter.assert_any_call(
            "mcp_tool_calls_succeeded_total",
            attributes={
                "server_name": "test-server",
                "tool_name": "test_tool",
                "success": "true",
                "project_id": "proj-1",
            },
        )
        mock_telemetry.observe_histogram.assert_called_once_with(
            "mcp_tool_call_duration_seconds",
            0.1,  # 100ms = 0.1s
            attributes={
                "server_name": "test-server",
                "tool_name": "test_tool",
                "success": "true",
                "project_id": "proj-1",
            },
        )

    def test_record_first_call_failure(
        self, metrics_manager: ToolMetricsManager, mock_telemetry
    ) -> None:
        """Test recording first failed call creates new record."""
        metrics_manager.record_call(
            server_name="test-server",
            tool_name="test_tool",
            project_id="proj-1",
            latency_ms=100.0,
            success=False,
        )

        result = metrics_manager.get_metrics(project_id="proj-1")
        assert result["summary"]["total_calls"] == 1
        assert result["summary"]["total_success"] == 0
        assert result["summary"]["total_failure"] == 1

        # Verify OTel calls
        mock_telemetry.inc_counter.assert_any_call(
            "mcp_tool_calls_total",
            attributes={
                "server_name": "test-server",
                "tool_name": "test_tool",
                "success": "false",
                "project_id": "proj-1",
            },
        )
        mock_telemetry.inc_counter.assert_any_call(
            "mcp_tool_calls_failed_total",
            attributes={
                "server_name": "test-server",
                "tool_name": "test_tool",
                "success": "false",
                "project_id": "proj-1",
            },
        )

    def test_record_multiple_calls_increments(self, metrics_manager: ToolMetricsManager) -> None:
        """Test multiple calls increment counters correctly."""
        # Record 3 successes and 2 failures
        for _ in range(3):
            metrics_manager.record_call(
                server_name="test-server",
                tool_name="test_tool",
                project_id="proj-1",
                latency_ms=100.0,
                success=True,
            )
        for _ in range(2):
            metrics_manager.record_call(
                server_name="test-server",
                tool_name="test_tool",
                project_id="proj-1",
                latency_ms=200.0,
                success=False,
            )

        result = metrics_manager.get_metrics(project_id="proj-1")
        assert result["summary"]["total_calls"] == 5
        assert result["summary"]["total_success"] == 3
        assert result["summary"]["total_failure"] == 2


class TestGetMetrics:
    """Tests for get_metrics method."""

    def test_get_metrics_empty_database(self, metrics_manager: ToolMetricsManager) -> None:
        """Test get_metrics returns empty result for empty database."""
        result = metrics_manager.get_metrics()
        assert result["tools"] == []
        assert result["summary"]["total_calls"] == 0

    def test_get_metrics_filter_by_project(self, metrics_manager: ToolMetricsManager) -> None:
        """Test filtering metrics by project_id."""
        metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)
        metrics_manager.record_call("server1", "tool1", "proj-2", 100.0, True)

        result = metrics_manager.get_metrics(project_id="proj-1")
        assert result["summary"]["total_calls"] == 1
        assert len(result["tools"]) == 1
        assert result["tools"][0]["project_id"] == "proj-1"

    def test_get_metrics_filter_by_server(self, metrics_manager: ToolMetricsManager) -> None:
        """Test filtering metrics by server_name."""
        metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)
        metrics_manager.record_call("server2", "tool2", "proj-1", 100.0, True)

        result = metrics_manager.get_metrics(server_name="server1")
        assert result["summary"]["total_calls"] == 1
        assert result["tools"][0]["server_name"] == "server1"

    def test_get_metrics_filter_by_tool(self, metrics_manager: ToolMetricsManager) -> None:
        """Test filtering metrics by tool_name."""
        metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)
        metrics_manager.record_call("server1", "tool2", "proj-1", 100.0, True)

        result = metrics_manager.get_metrics(tool_name="tool1")
        assert result["summary"]["total_calls"] == 1
        assert result["tools"][0]["tool_name"] == "tool1"

    def test_get_metrics_aggregates(self, metrics_manager: ToolMetricsManager) -> None:
        """Test aggregate calculations in get_metrics."""
        # Record calls with different latencies
        metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)
        metrics_manager.record_call("server1", "tool1", "proj-1", 200.0, True)
        metrics_manager.record_call("server1", "tool1", "proj-1", 300.0, False)

        result = metrics_manager.get_metrics(project_id="proj-1")
        summary = result["summary"]
        assert summary["total_calls"] == 3
        assert summary["total_success"] == 2
        assert summary["total_failure"] == 1
        assert summary["overall_success_rate"] == pytest.approx(2 / 3, rel=0.01)
        assert summary["overall_avg_latency_ms"] == pytest.approx(200.0, rel=0.01)


class TestGetTopTools:
    """Tests for get_top_tools method."""

    def test_get_top_tools_empty_database(self, metrics_manager: ToolMetricsManager) -> None:
        """Test get_top_tools returns empty list for empty database."""
        result = metrics_manager.get_top_tools()
        assert result == []

    def test_get_top_tools_by_call_count(self, metrics_manager: ToolMetricsManager) -> None:
        """Test ordering tools by call_count."""
        # Record different numbers of calls for different tools
        for _ in range(5):
            metrics_manager.record_call("server1", "popular_tool", "proj-1", 100.0, True)
        for _ in range(2):
            metrics_manager.record_call("server1", "less_popular", "proj-1", 100.0, True)

        result = metrics_manager.get_top_tools(order_by="call_count")
        assert len(result) == 2
        assert result[0]["tool_name"] == "popular_tool"
        assert result[0]["call_count"] == 5

    def test_get_top_tools_with_limit(self, metrics_manager: ToolMetricsManager) -> None:
        """Test limit parameter works correctly."""
        for i in range(5):
            metrics_manager.record_call("server1", f"tool{i}", "proj-1", 100.0, True)

        result = metrics_manager.get_top_tools(limit=3)
        assert len(result) == 3

    def test_get_top_tools_invalid_order_falls_back(
        self, metrics_manager: ToolMetricsManager
    ) -> None:
        """Test invalid order_by falls back to call_count."""
        metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)

        # Should not raise, should fall back to call_count
        result = metrics_manager.get_top_tools(order_by="invalid_column")
        assert len(result) == 1

    def test_get_top_tools_filter_by_project(self, metrics_manager: ToolMetricsManager) -> None:
        """Test filtering by project_id."""
        metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)
        metrics_manager.record_call("server1", "tool2", "proj-2", 100.0, True)

        result = metrics_manager.get_top_tools(project_id="proj-1")
        assert len(result) == 1
        assert result[0]["project_id"] == "proj-1"


class TestGetToolSuccessRate:
    """Tests for get_tool_success_rate method."""

    def test_success_rate_nonexistent_tool(self, metrics_manager: ToolMetricsManager) -> None:
        """Test success rate returns None for nonexistent tool."""
        result = metrics_manager.get_tool_success_rate(
            server_name="server1",
            tool_name="nonexistent",
            project_id="proj-1",
        )
        assert result is None

    def test_success_rate_calculation(self, metrics_manager: ToolMetricsManager) -> None:
        """Test success rate calculation."""
        # Record 8 successes and 2 failures
        for _ in range(8):
            metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)
        for _ in range(2):
            metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, False)

        result = metrics_manager.get_tool_success_rate(
            server_name="server1",
            tool_name="tool1",
            project_id="proj-1",
        )
        assert result == pytest.approx(0.8, rel=0.01)

    def test_success_rate_all_success(self, metrics_manager: ToolMetricsManager) -> None:
        """Test success rate with all successes."""
        for _ in range(5):
            metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)

        result = metrics_manager.get_tool_success_rate(
            server_name="server1",
            tool_name="tool1",
            project_id="proj-1",
        )
        assert result == 1.0

    def test_success_rate_all_failures(self, metrics_manager: ToolMetricsManager) -> None:
        """Test success rate with all failures."""
        for _ in range(5):
            metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, False)

        result = metrics_manager.get_tool_success_rate(
            server_name="server1",
            tool_name="tool1",
            project_id="proj-1",
        )
        assert result == 0.0


class TestGetFailingTools:
    """Tests for get_failing_tools method."""

    def test_get_failing_tools_empty(self, metrics_manager: ToolMetricsManager) -> None:
        """Test get_failing_tools returns empty list when no failures."""
        result = metrics_manager.get_failing_tools()
        assert result == []

    def test_get_failing_tools_above_threshold(self, metrics_manager: ToolMetricsManager) -> None:
        """Test get_failing_tools filters by threshold."""
        # Tool with 60% failure rate (above default 50% threshold)
        for _ in range(4):
            metrics_manager.record_call("server1", "failing_tool", "proj-1", 100.0, False)
        for _ in range(6):
            metrics_manager.record_call("server1", "failing_tool", "proj-1", 100.0, True)

        # Tool with 20% failure rate (below threshold)
        for _ in range(2):
            metrics_manager.record_call("server1", "good_tool", "proj-1", 100.0, False)
        for _ in range(8):
            metrics_manager.record_call("server1", "good_tool", "proj-1", 100.0, True)

        result = metrics_manager.get_failing_tools(threshold=0.3)
        assert len(result) == 1
        assert result[0]["tool_name"] == "failing_tool"
        assert result[0]["failure_rate"] == pytest.approx(0.4, rel=0.01)

    def test_get_failing_tools_filter_by_project(self, metrics_manager: ToolMetricsManager) -> None:
        """Test filtering failing tools by project."""
        for _ in range(5):
            metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, False)
        for _ in range(5):
            metrics_manager.record_call("server1", "tool2", "proj-2", 100.0, False)

        result = metrics_manager.get_failing_tools(project_id="proj-1", threshold=0.5)
        assert len(result) == 1
        assert result[0]["project_id"] == "proj-1"

    @pytest.mark.integration
    def test_get_failing_tools_respects_limit(self, metrics_manager: ToolMetricsManager) -> None:
        """Test limit parameter works correctly."""
        for i in range(5):
            for _ in range(5):
                metrics_manager.record_call("server1", f"failing_tool{i}", "proj-1", 100.0, False)

        result = metrics_manager.get_failing_tools(threshold=0.5, limit=3)
        assert len(result) == 3


class TestResetMetrics:
    """Tests for reset_metrics method."""

    def test_reset_all_metrics(self, metrics_manager: ToolMetricsManager) -> None:
        """Test resetting all metrics."""
        metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)
        metrics_manager.record_call("server2", "tool2", "proj-2", 100.0, True)

        deleted = metrics_manager.reset_metrics()
        assert deleted == 2

        result = metrics_manager.get_metrics()
        assert result["summary"]["total_calls"] == 0

    def test_reset_by_project(self, metrics_manager: ToolMetricsManager) -> None:
        """Test resetting metrics for specific project."""
        metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)
        metrics_manager.record_call("server1", "tool2", "proj-2", 100.0, True)

        deleted = metrics_manager.reset_metrics(project_id="proj-1")
        assert deleted == 1

        result = metrics_manager.get_metrics()
        assert result["summary"]["total_calls"] == 1
        assert result["tools"][0]["project_id"] == "proj-2"

    def test_reset_by_server(self, metrics_manager: ToolMetricsManager) -> None:
        """Test resetting metrics for specific server."""
        metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)
        metrics_manager.record_call("server2", "tool2", "proj-1", 100.0, True)

        deleted = metrics_manager.reset_metrics(server_name="server1")
        assert deleted == 1

        result = metrics_manager.get_metrics()
        assert result["tools"][0]["server_name"] == "server2"


class TestCleanupOldMetrics:
    """Tests for cleanup_old_metrics method."""

    def test_cleanup_old_metrics(
        self, metrics_manager: ToolMetricsManager, temp_db: "HubDatabase"
    ) -> None:
        """Test cleanup deletes old metrics."""
        # Insert a metric with old timestamp
        old_time = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        temp_db.execute(
            """
            INSERT INTO tool_metrics (
                id, project_id, server_name, tool_name,
                call_count, success_count, failure_count,
                total_latency_ms, avg_latency_ms,
                last_called_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tm-old",
                "proj-1",
                "server1",
                "old_tool",
                1,
                1,
                0,
                100.0,
                100.0,
                old_time,
                old_time,
                old_time,
            ),
        )

        # Record a new metric
        metrics_manager.record_call("server1", "new_tool", "proj-1", 100.0, True)

        # Cleanup with 7 day retention
        deleted = metrics_manager.cleanup_old_metrics(retention_days=7)
        assert deleted == 1

        # Verify only new metric remains
        result = metrics_manager.get_metrics()
        assert result["summary"]["total_calls"] == 1
        assert result["tools"][0]["tool_name"] == "new_tool"


class TestGetRetentionStats:
    """Tests for get_retention_stats method."""

    def test_retention_stats_empty(self, metrics_manager: ToolMetricsManager) -> None:
        """Test retention stats for empty database."""
        result = metrics_manager.get_retention_stats()
        assert result["total_metrics"] == 0
        assert result["oldest_metric"] is None
        assert result["newest_metric"] is None
        assert result["total_calls_recorded"] is None or result["total_calls_recorded"] == 0

    def test_retention_stats_with_data(self, metrics_manager: ToolMetricsManager) -> None:
        """Test retention stats with data."""
        # Record some calls
        metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)
        metrics_manager.record_call("server1", "tool2", "proj-1", 100.0, True)
        metrics_manager.record_call("server1", "tool1", "proj-1", 100.0, True)

        result = metrics_manager.get_retention_stats()
        assert result["total_metrics"] == 2  # 2 unique tool entries
        assert result["total_calls_recorded"] == 3  # Total calls
        assert result["oldest_metric"] is not None
        assert result["newest_metric"] is not None


class TestGetDailyMetrics:
    """Tests for get_daily_metrics method."""

    def test_get_daily_metrics_empty(self, metrics_manager: ToolMetricsManager) -> None:
        """Test get_daily_metrics returns empty for no daily data."""
        result = metrics_manager.get_daily_metrics()
        assert result["daily"] == []
        assert result["summary"]["total_days"] == 0

    def test_get_daily_metrics_with_filters(
        self, metrics_manager: ToolMetricsManager, temp_db: "HubDatabase"
    ) -> None:
        """Test get_daily_metrics with filters."""
        # Insert daily metrics directly
        temp_db.execute(
            """
            INSERT INTO tool_metrics_daily (
                project_id, server_name, tool_name, date,
                call_count, success_count, failure_count,
                total_latency_ms, avg_latency_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            ("proj-1", "server1", "tool1", "2024-01-01", 10, 8, 2, 1000.0, 100.0),
        )
        temp_db.execute(
            """
            INSERT INTO tool_metrics_daily (
                project_id, server_name, tool_name, date,
                call_count, success_count, failure_count,
                total_latency_ms, avg_latency_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            ("proj-2", "server2", "tool2", "2024-01-02", 5, 5, 0, 500.0, 100.0),
        )

        # Filter by project
        result = metrics_manager.get_daily_metrics(project_id="proj-1")
        assert len(result["daily"]) == 1
        assert result["daily"][0]["project_id"] == "proj-1"

        # Filter by date range
        result = metrics_manager.get_daily_metrics(start_date="2024-01-01", end_date="2024-01-01")
        assert len(result["daily"]) == 1
        assert result["daily"][0]["date"] == "2024-01-01"
