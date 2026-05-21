"""Tool metrics tracking for MCP proxy."""

import logging
from typing import Any

from gobby.mcp_proxy.metrics_events import MetricsEventStore
from gobby.mcp_proxy.metrics_store import ToolMetrics, ToolMetricsStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.telemetry.instruments import get_telemetry_metrics

logger = logging.getLogger(__name__)

# Default retention period for metrics
DEFAULT_RETENTION_DAYS = 7


class ToolMetricsManager:
    """
    Manager for tracking tool call metrics.

    Refactored to a facade that dual-writes to OTel (for real-time observability)
    and hub database tables (for queryable analytics).
    """

    def __init__(
        self,
        db: HubDatabase,
        event_store: MetricsEventStore | None = None,
    ):
        """
        Initialize the metrics manager.

        Args:
            db: Hub database adapter for persistence
            event_store: Optional MetricsEventStore for per-event recording
        """
        self.store = ToolMetricsStore(db)
        self.event_store = event_store or MetricsEventStore(db)
        self.metrics = get_telemetry_metrics()

    def record_call(
        self,
        server_name: str,
        tool_name: str,
        project_id: str,
        latency_ms: float,
        success: bool = True,
        session_id: str | None = None,
    ) -> None:
        """
        Record a tool call with its metrics.
        Triple-writes to: hub aggregate table, event log, and OTel.
        """
        # 1. Hub database aggregate persistence.
        try:
            self.store.record_call(
                server_name=server_name,
                tool_name=tool_name,
                project_id=project_id,
                latency_ms=latency_ms,
                success=success,
            )
        except Exception as e:
            logger.error(f"Failed to record call to hub database: {e}")

        # 2. Event log (per-event with session_id)
        try:
            self.event_store.record_event(
                event_type="tool_call",
                name=tool_name,
                project_id=project_id,
                session_id=session_id,
                server_name=server_name,
                success=success,
                latency_ms=latency_ms,
            )
        except Exception as e:
            logger.warning(f"Failed to record event: {e}")

        # 3. OTel Observability
        attributes = {
            "server_name": server_name,
            "tool_name": tool_name,
            "success": str(success).lower(),
            "project_id": project_id,
        }
        if session_id:
            attributes["session_id"] = session_id

        # Increment total calls
        self.metrics.inc_counter("mcp_tool_calls_total", attributes=attributes)

        # Increment success/failure specific counters
        if success:
            self.metrics.inc_counter("mcp_tool_calls_succeeded_total", attributes=attributes)
        else:
            self.metrics.inc_counter("mcp_tool_calls_failed_total", attributes=attributes)

        # Record latency (convert ms to seconds for OTel convention)
        self.metrics.observe_histogram(
            "mcp_tool_call_duration_seconds", latency_ms / 1000.0, attributes=attributes
        )

    def get_metrics(
        self,
        project_id: str | None = None,
        server_name: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Get metrics, optionally filtered by project/server/tool.
        Delegates to ToolMetricsStore.
        """
        rows = self.store.get_metrics(project_id, server_name, tool_name)
        tools = [ToolMetrics.from_row(row).to_dict() for row in rows]

        # Calculate aggregates
        total_calls = sum(t["call_count"] for t in tools)
        total_success = sum(t["success_count"] for t in tools)
        total_failure = sum(t["failure_count"] for t in tools)
        total_latency = sum(t["total_latency_ms"] for t in tools)

        return {
            "tools": tools,
            "summary": {
                "total_tools": len(tools),
                "total_calls": total_calls,
                "total_success": total_success,
                "total_failure": total_failure,
                "overall_success_rate": (total_success / total_calls if total_calls > 0 else None),
                "overall_avg_latency_ms": (
                    total_latency / total_calls if total_calls > 0 else None
                ),
            },
        }

    def get_top_tools(
        self,
        project_id: str | None = None,
        limit: int = 10,
        order_by: str = "call_count",
    ) -> list[dict[str, Any]]:
        """
        Get top tools by call count or other metrics.
        Delegates to ToolMetricsStore.
        """
        rows = self.store.get_top_tools(project_id, limit, order_by)
        return [ToolMetrics.from_row(row).to_dict() for row in rows]

    def get_tool_success_rate(
        self,
        server_name: str,
        tool_name: str,
        project_id: str,
    ) -> float | None:
        """
        Get success rate for a specific tool.
        Delegates to ToolMetricsStore.
        """
        return self.store.get_tool_success_rate(server_name, tool_name, project_id)

    def get_failing_tools(
        self,
        project_id: str | None = None,
        threshold: float = 0.5,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Get tools with failure rate above a threshold.
        Delegates to ToolMetricsStore.
        """
        rows = self.store.get_failing_tools(project_id, threshold, limit)
        result = []
        for row in rows:
            tool_dict = ToolMetrics.from_row(row).to_dict()
            tool_dict["failure_rate"] = row["failure_rate"]
            result.append(tool_dict)
        return result

    def reset_metrics(
        self,
        project_id: str | None = None,
        server_name: str | None = None,
        tool_name: str | None = None,
    ) -> int:
        """
        Reset/delete metrics.
        Delegates to ToolMetricsStore.
        """
        return self.store.reset_metrics(project_id, server_name, tool_name)

    def aggregate_to_daily(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """
        Aggregate old metrics into daily summaries.
        Delegates to ToolMetricsStore.
        """
        return self.store.aggregate_to_daily(retention_days)

    def cleanup_old_metrics(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """
        Aggregate and delete metrics older than the retention period.
        Delegates to ToolMetricsStore.
        """
        # First aggregate to daily table
        self.store.aggregate_to_daily(retention_days)
        # Then cleanup
        return self.store.cleanup_old_metrics(retention_days)

    def get_daily_metrics(
        self,
        project_id: str | None = None,
        server_name: str | None = None,
        tool_name: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """
        Get aggregated daily metrics for historical analysis.
        Delegates to ToolMetricsStore.
        """
        rows = self.store.get_daily_metrics(
            project_id, server_name, tool_name, start_date, end_date
        )

        daily_data = []
        for row in rows:
            call_count = row["call_count"]
            daily_data.append(
                {
                    "project_id": row["project_id"],
                    "server_name": row["server_name"],
                    "tool_name": row["tool_name"],
                    "date": row["date"],
                    "call_count": call_count,
                    "success_count": row["success_count"],
                    "failure_count": row["failure_count"],
                    "total_latency_ms": row["total_latency_ms"],
                    "avg_latency_ms": row["avg_latency_ms"],
                    "success_rate": (row["success_count"] / call_count if call_count > 0 else None),
                }
            )

        # Calculate aggregates
        total_calls = sum(d["call_count"] for d in daily_data)
        total_success = sum(d["success_count"] for d in daily_data)
        total_latency = sum(d["total_latency_ms"] for d in daily_data)

        return {
            "daily": daily_data,
            "summary": {
                "total_days": len({d["date"] for d in daily_data}),
                "total_calls": total_calls,
                "total_success": total_success,
                "overall_success_rate": (total_success / total_calls if total_calls > 0 else None),
                "overall_avg_latency_ms": (
                    total_latency / total_calls if total_calls > 0 else None
                ),
            },
        }

    def get_retention_stats(self) -> dict[str, Any]:
        """
        Get statistics about metrics retention.
        Delegates to ToolMetricsStore.
        """
        row = self.store.get_retention_stats()
        if row:
            return {
                "total_metrics": row["total_count"],
                "oldest_metric": row["oldest"],
                "newest_metric": row["newest"],
                "total_calls_recorded": row["total_calls"],
            }
        return {
            "total_metrics": 0,
            "oldest_metric": None,
            "newest_metric": None,
            "total_calls_recorded": 0,
        }
