"""Session-based token tracking for budget management.

This module provides SessionTokenTracker which aggregates usage from sessions
over time and enables budget tracking for agent spawning decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SessionTokenTracker:
    """Track token usage from sessions for budget management.

    This class aggregates usage data from sessions over time and provides
    budget checking for agent spawning decisions.

    Example:
        tracker = SessionTokenTracker(
            session_storage=session_manager,
            daily_budget_usd=10.0,
        )

        # Get usage summary for last 7 days
        summary = tracker.get_usage_summary(days=7)

        # Check if we can spawn an agent
        can_spawn, reason = tracker.can_spawn_agent()
    """

    session_storage: Any  # LocalSessionManager
    daily_budget_usd: float = 50.0  # Default daily budget in USD

    def get_usage_summary(self, days: int = 1) -> dict[str, Any]:
        """Get usage summary for the specified number of days.

        Args:
            days: Number of days to look back (default: 1 = today)

        Returns:
            Dict with total cost, tokens, session count, and model breakdown
        """
        since = datetime.now(UTC) - timedelta(days=days)
        sessions = self.session_storage.get_sessions_since(since)

        total_cost = 0.0
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_creation_tokens = 0
        total_cache_read_tokens = 0
        usage_by_model: dict[str, dict[str, Any]] = {}

        for session in sessions:
            # Safely handle None values by defaulting to 0
            total_cost += session.usage_total_cost_usd or 0
            total_input_tokens += session.usage_input_tokens or 0
            total_output_tokens += session.usage_output_tokens or 0
            total_cache_creation_tokens += session.usage_cache_creation_tokens or 0
            total_cache_read_tokens += session.usage_cache_read_tokens or 0

            # Aggregate by model
            model = session.model or "unknown"
            if model not in usage_by_model:
                usage_by_model[model] = {
                    "cost": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "sessions": 0,
                }

            usage_by_model[model]["cost"] += session.usage_total_cost_usd or 0
            usage_by_model[model]["input_tokens"] += session.usage_input_tokens or 0
            usage_by_model[model]["output_tokens"] += session.usage_output_tokens or 0
            usage_by_model[model]["sessions"] += 1

        return {
            "total_cost_usd": total_cost,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "session_count": len(sessions),
            "usage_by_model": usage_by_model,
            "period_days": days,
        }

    def get_budget_status(self) -> dict[str, Any]:
        """Get current budget status for today.

        Returns:
            Dict with budget info: daily_budget_usd, used_today_usd,
            remaining_usd, percentage_used, over_budget
        """
        summary = self.get_usage_summary(days=1)
        used_today = summary["total_cost_usd"]

        # Handle unlimited budget (daily_budget_usd <= 0)
        if self.daily_budget_usd <= 0:
            return {
                "daily_budget_usd": self.daily_budget_usd,
                "used_today_usd": used_today,
                "remaining_usd": float("inf"),
                "percentage_used": 0.0,
                "over_budget": False,
            }

        remaining = self.daily_budget_usd - used_today

        return {
            "daily_budget_usd": self.daily_budget_usd,
            "used_today_usd": used_today,
            "remaining_usd": remaining,
            "percentage_used": (used_today / self.daily_budget_usd * 100),
            "over_budget": used_today > self.daily_budget_usd,
        }

    def can_spawn_agent(self, estimated_cost: float | None = None) -> tuple[bool, str | None]:
        """Check if we can spawn an agent based on budget.

        Args:
            estimated_cost: Optional estimated cost for the agent run

        Returns:
            Tuple of (can_spawn, reason if not)
        """
        # Unlimited budget (0 or negative means no limit)
        if self.daily_budget_usd <= 0.0:
            return True, None

        status = self.get_budget_status()

        # Already over budget
        if status["over_budget"]:
            return (
                False,
                f"Daily budget exceeded: ${status['used_today_usd']:.2f} used "
                f"of ${self.daily_budget_usd:.2f}",
            )

        # Check if estimated cost would exceed budget
        if estimated_cost is not None:
            if status["used_today_usd"] + estimated_cost > self.daily_budget_usd:
                return (
                    False,
                    f"Estimated cost ${estimated_cost:.2f} would exceed budget. "
                    f"Remaining: ${status['remaining_usd']:.2f}",
                )

        return True, None
