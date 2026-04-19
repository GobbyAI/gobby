"""
Internal MCP tools for Tool Metrics.

Exposes functionality for:
- Querying tool call metrics (get_tool_metrics)
- Getting top performing tools (get_top_tools)

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.mcp_proxy.metrics import ToolMetricsManager
from gobby.mcp_proxy.metrics_events import MetricsEventStore
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.sessions.token_tracker import SessionTokenTracker


def create_metrics_registry(
    metrics_manager: ToolMetricsManager,
    session_storage: Any | None = None,
    event_store: MetricsEventStore | None = None,
) -> InternalToolRegistry:
    """
    Create a metrics tool registry with all metrics-related tools.

    Args:
        metrics_manager: ToolMetricsManager instance
        session_storage: Optional LocalSessionManager for usage reporting

    Returns:
        InternalToolRegistry with metrics tools registered
    """
    # Create usage tracker if session storage is provided
    token_tracker: SessionTokenTracker | None = None
    if session_storage is not None:
        token_tracker = SessionTokenTracker(
            db=getattr(session_storage, "db", None),
            session_storage=session_storage,
        )
    registry = InternalToolRegistry(
        name="gobby-metrics",
        description="Tool metrics - query call counts, success rates, latency",
    )

    @registry.tool(
        name="get_tool_metrics",
        description="Get metrics for MCP tools including call count, success rate, and latency.",
    )
    def get_tool_metrics(
        server_name: str | None = None,
        tool_name: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Get metrics for MCP tools.

        Args:
            server_name: Optional server name to filter by
            tool_name: Optional tool name to filter by
            project_id: Optional project ID to filter by

        Returns:
            Dictionary with tool metrics including call counts, success rates, and latency
        """
        try:
            result = metrics_manager.get_metrics(
                project_id=project_id,
                server_name=server_name,
                tool_name=tool_name,
            )
            return {"success": True, "metrics": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_top_tools",
        description="Get top tools by usage, success rate, or latency.",
    )
    def get_top_tools(
        project_id: str | None = None,
        limit: int = 10,
        order_by: str = "call_count",
    ) -> dict[str, Any]:
        """
        Get top tools by various metrics.

        Args:
            project_id: Optional project ID to filter by
            limit: Maximum number of tools to return (default: 10)
            order_by: Sort criteria - "call_count", "success_count", or "avg_latency_ms"

        Returns:
            List of top tools with their metrics
        """
        try:
            tools = metrics_manager.get_top_tools(
                project_id=project_id,
                limit=limit,
                order_by=order_by,
            )
            return {
                "success": True,
                "tools": tools,
                "count": len(tools),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_failing_tools",
        description="Get tools with high failure rates above a threshold.",
    )
    def get_failing_tools(
        project_id: str | None = None,
        threshold: float = 0.5,
        limit: int = 10,
    ) -> dict[str, Any]:
        """
        Get tools with failure rate above a threshold.

        Args:
            project_id: Optional project ID to filter by
            threshold: Minimum failure rate (0.0-1.0) to include a tool (default: 0.5)
            limit: Maximum number of tools to return (default: 10)

        Returns:
            List of failing tools sorted by failure rate descending
        """
        try:
            tools = metrics_manager.get_failing_tools(
                project_id=project_id,
                threshold=threshold,
                limit=limit,
            )
            return {
                "success": True,
                "tools": tools,
                "count": len(tools),
                "threshold": threshold,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_tool_success_rate",
        description="Get success rate for a specific tool.",
    )
    def get_tool_success_rate(
        server_name: str,
        tool_name: str,
        project_id: str,
    ) -> dict[str, Any]:
        """
        Get success rate for a specific tool.

        Args:
            server_name: Name of the MCP server
            tool_name: Name of the tool
            project_id: Project ID

        Returns:
            Success rate as a float between 0 and 1
        """
        try:
            rate = metrics_manager.get_tool_success_rate(
                server_name=server_name,
                tool_name=tool_name,
                project_id=project_id,
            )
            return {
                "success": True,
                "server_name": server_name,
                "tool_name": tool_name,
                "success_rate": rate,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="reset_metrics",
        description="Reset/delete metrics for a project, server, or specific tool.",
    )
    def reset_metrics(
        project_id: str | None = None,
        server_name: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Reset/delete metrics.

        Args:
            project_id: Reset only for this project
            server_name: Reset only for this server
            tool_name: Reset only for this specific tool

        Returns:
            Number of rows deleted
        """
        try:
            deleted = metrics_manager.reset_metrics(
                project_id=project_id,
                server_name=server_name,
                tool_name=tool_name,
            )
            return {"success": True, "deleted_count": deleted}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="reset_tool_metrics",
        description="Admin tool to reset/delete metrics for a specific tool.",
    )
    def reset_tool_metrics(
        server_name: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Reset/delete metrics for a specific tool (admin operation).

        Args:
            server_name: Server containing the tool
            tool_name: Specific tool to reset metrics for

        Returns:
            Number of rows deleted
        """
        try:
            deleted = metrics_manager.reset_metrics(
                server_name=server_name,
                tool_name=tool_name,
            )
            return {
                "success": True,
                "deleted_count": deleted,
                "server_name": server_name,
                "tool_name": tool_name,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="cleanup_old_metrics",
        description="Delete metrics older than retention period (default 7 days).",
    )
    def cleanup_old_metrics(
        retention_days: int = 7,
    ) -> dict[str, Any]:
        """
        Delete metrics older than the retention period.

        Args:
            retention_days: Number of days to retain metrics (default: 7)

        Returns:
            Number of rows deleted
        """
        try:
            deleted = metrics_manager.cleanup_old_metrics(
                retention_days=retention_days,
            )
            return {
                "success": True,
                "deleted_count": deleted,
                "retention_days": retention_days,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_retention_stats",
        description="Get statistics about metrics retention and age.",
    )
    def get_retention_stats() -> dict[str, Any]:
        """
        Get statistics about metrics retention.

        Returns:
            Dictionary with retention statistics
        """
        try:
            stats = metrics_manager.get_retention_stats()
            return {"success": True, "stats": stats}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Usage reporting tools (only available if session_storage provided)
    @registry.tool(
        name="get_usage_report",
        description="Get token usage report for a specified time period.",
    )
    def get_usage_report(days: int = 1) -> dict[str, Any]:
        """
        Get usage report including token counts and costs.

        Args:
            days: Number of days to look back (default: 1 = today)

        Returns:
            Dictionary with usage summary
        """
        if token_tracker is None:
            return {"success": False, "error": "Usage reporting not configured"}

        try:
            summary = token_tracker.get_usage_summary(days=days)
            return {"success": True, "usage": summary}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --- Event-based metrics tools (require event_store) ---

    @registry.tool(
        name="get_session_tools",
        description="Get per-tool call breakdown for a specific session.",
    )
    def get_session_tools(session_id: str) -> dict[str, Any]:
        """Get which tools a session used and how often.

        Args:
            session_id: Session ID to query.

        Returns:
            Per-tool breakdown with call counts, success rates, and latency.
        """
        if event_store is None:
            return {"success": False, "error": "Event store not configured"}
        try:
            breakdown = event_store.get_session_tool_breakdown(session_id)
            return {
                "success": True,
                "session_id": session_id,
                "tools": breakdown,
                "total_tools": len(breakdown),
                "total_calls": sum(t["call_count"] for t in breakdown),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_rule_metrics",
        description="Get rule evaluation stats: which rules fire, block vs allow counts, latency.",
    )
    def get_rule_metrics(
        hours: int = 24,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Get aggregate rule evaluation metrics.

        Args:
            hours: Lookback period in hours (default: 24).
            session_id: Optional session ID to filter by.

        Returns:
            Per-rule stats with eval count, block/allow breakdown, latency.
        """
        if event_store is None:
            return {"success": False, "error": "Event store not configured"}
        try:
            since = datetime.now(UTC) - timedelta(hours=hours)
            stats = event_store.get_rule_stats(since=since, session_id=session_id)
            total_evals = sum(r["eval_count"] for r in stats)
            total_blocks = sum(r["block_count"] for r in stats)
            return {
                "success": True,
                "hours": hours,
                "rules": stats,
                "summary": {
                    "total_evals": total_evals,
                    "total_blocks": total_blocks,
                    "total_allows": total_evals - total_blocks,
                    "unique_rules": len(stats),
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_skill_metrics",
        description="Get skill search and invocation stats.",
    )
    def get_skill_metrics(
        hours: int = 24,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Get aggregate skill usage metrics.

        Args:
            hours: Lookback period in hours (default: 24).
            session_id: Optional session ID to filter by.

        Returns:
            Per-skill stats with search and invoke counts.
        """
        if event_store is None:
            return {"success": False, "error": "Event store not configured"}
        try:
            since = datetime.now(UTC) - timedelta(hours=hours)
            stats = event_store.get_skill_stats(since=since, session_id=session_id)
            searches = sum(s["count"] for s in stats if s["event_type"] == "skill_search")
            invocations = sum(s["count"] for s in stats if s["event_type"] == "skill_invoke")
            return {
                "success": True,
                "hours": hours,
                "skills": stats,
                "summary": {
                    "total_searches": searches,
                    "total_invocations": invocations,
                    "unique_skills": len({s["skill_name"] for s in stats}),
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_metrics_timeseries",
        description="Get time-bucketed metrics for dashboard charts. Supports 1h/6h/12h/24h/7d/30d/all ranges.",
    )
    def get_metrics_timeseries(
        event_type: str = "tool_call",
        time_range: str = "24h",
        name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Get time-series metrics data.

        Args:
            event_type: Event type to query (tool_call, rule_eval, skill_search, skill_invoke).
            time_range: Time range - 1h, 6h, 12h, 24h, 7d, 30d, or all.
            name: Optional tool/rule/skill name to filter by.
            session_id: Optional session ID to filter by.

        Returns:
            Time-bucketed data with call counts, success/failure, latency.
        """
        if event_store is None:
            return {"success": False, "error": "Event store not configured"}
        try:
            result = event_store.get_timeseries(
                event_type=event_type,
                range_key=time_range,
                name=name,
                session_id=session_id,
            )
            return {"success": True, **result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return registry
