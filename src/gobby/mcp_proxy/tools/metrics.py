"""
Internal MCP tools for Tool Metrics.

Exposes functionality for:
- Querying tool call metrics (get_tool_metrics)
- Getting top performing tools (get_top_tools)

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool).
"""

from typing import Any

from gobby.mcp_proxy.metrics import ToolMetricsManager
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.sessions.token_tracker import SessionTokenTracker


def create_metrics_registry(
    metrics_manager: ToolMetricsManager,
    session_storage: Any | None = None,
    daily_budget_usd: float = 50.0,
) -> InternalToolRegistry:
    """
    Create a metrics tool registry with all metrics-related tools.

    Args:
        metrics_manager: ToolMetricsManager instance
        session_storage: Optional LocalSessionManager for token/cost tracking
        daily_budget_usd: Daily budget limit for token tracking (default: $50)

    Returns:
        InternalToolRegistry with metrics tools registered
    """
    # Create token tracker if session storage is provided
    token_tracker: SessionTokenTracker | None = None
    if session_storage is not None:
        token_tracker = SessionTokenTracker(
            session_storage=session_storage,
            daily_budget_usd=daily_budget_usd,
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

    # Token/cost tracking tools (only available if session_storage provided)
    @registry.tool(
        name="get_usage_report",
        description="Get token and cost usage report for a specified time period.",
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
            return {"success": False, "error": "Token tracking not configured"}

        try:
            summary = token_tracker.get_usage_summary(days=days)
            return {"success": True, "usage": summary}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_budget_status",
        description="Get current daily budget status including used amount and remaining budget.",
    )
    def get_budget_status() -> dict[str, Any]:
        """
        Get current budget status for today.

        Returns:
            Dictionary with budget info
        """
        if token_tracker is None:
            return {"success": False, "error": "Token tracking not configured"}

        try:
            status = token_tracker.get_budget_status()
            return {"success": True, "budget": status}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return registry
