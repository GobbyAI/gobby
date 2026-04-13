"""Session-based token tracking for budget management.

This module provides SessionTokenTracker which aggregates usage from sessions
over time and enables budget tracking for agent spawning decisions.

Budget is expressed in tokens/day, not USD.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from gobby.storage.session_models import Session

logger = logging.getLogger(__name__)


def _format_tokens(n: int) -> str:
    """Format token count for human display (e.g., 10.5M, 1.2K)."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


class SessionStorageProtocol(Protocol):
    """Protocol for the session storage dependency used by token tracking."""

    def get_sessions_since(
        self, since: datetime, project_id: str | None = None
    ) -> list[Session]: ...


@dataclass
class SessionTokenTracker:
    """Track token usage from sessions for budget management.

    This class aggregates usage data from sessions over time and provides
    budget checking for agent spawning decisions.

    Example:
        tracker = SessionTokenTracker(
            session_storage=session_manager,
            daily_budget_tokens=10_000_000,
        )

        # Get usage summary for last 7 days
        summary = tracker.get_usage_summary(days=7)

        # Check if we can spawn an agent
        can_spawn, reason = tracker.can_spawn_agent()
    """

    session_storage: SessionStorageProtocol
    daily_budget_tokens: int = 10_000_000  # Default daily budget: 10M tokens

    def get_usage_summary(self, days: int = 1, project_id: str | None = None) -> dict[str, Any]:
        """Get usage summary for the specified number of days.

        Args:
            days: Number of days to look back (default: 1 = today)
            project_id: Optional project ID to filter by

        Returns:
            Dict with total tokens, session count, and breakdowns
            by model and source
        """
        since = datetime.now(UTC) - timedelta(days=days)
        sessions = self.session_storage.get_sessions_since(since, project_id=project_id)

        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_creation_tokens = 0
        total_cache_read_tokens = 0
        usage_by_model: dict[str, dict[str, Any]] = {}
        usage_by_source: dict[str, dict[str, Any]] = {}

        for session in sessions:
            inp = session.usage_input_tokens or 0
            out = session.usage_output_tokens or 0
            cache_create = session.usage_cache_creation_tokens or 0
            cache_read = session.usage_cache_read_tokens or 0

            total_input_tokens += inp
            total_output_tokens += out
            total_cache_creation_tokens += cache_create
            total_cache_read_tokens += cache_read

            # Aggregate by model
            model = session.model or "unknown"
            if model not in usage_by_model:
                usage_by_model[model] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "sessions": 0,
                }
            usage_by_model[model]["input_tokens"] += inp
            usage_by_model[model]["output_tokens"] += out
            usage_by_model[model]["sessions"] += 1

            # Aggregate by source (CLI adapter)
            source = getattr(session, "source", None) or "unknown"
            if source not in usage_by_source:
                usage_by_source[source] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                    "sessions": 0,
                }
            usage_by_source[source]["input_tokens"] += inp
            usage_by_source[source]["output_tokens"] += out
            usage_by_source[source]["cache_creation_tokens"] += cache_create
            usage_by_source[source]["cache_read_tokens"] += cache_read
            usage_by_source[source]["sessions"] += 1

        return {
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "session_count": len(sessions),
            "usage_by_model": usage_by_model,
            "usage_by_source": usage_by_source,
            "period_days": days,
        }

    def get_budget_status(self) -> dict[str, Any]:
        """Get current budget status for today.

        Returns:
            Dict with budget info: daily_budget_tokens, used_today_tokens,
            remaining_tokens, percentage_used, over_budget
        """
        summary = self.get_usage_summary(days=1)
        used_today = summary["total_input_tokens"] + summary["total_output_tokens"]

        # Handle unlimited budget (daily_budget_tokens <= 0)
        if self.daily_budget_tokens <= 0:
            return {
                "daily_budget_tokens": self.daily_budget_tokens,
                "used_today_tokens": used_today,
                "remaining_tokens": float("inf"),
                "percentage_used": 0.0,
                "over_budget": False,
            }

        remaining = self.daily_budget_tokens - used_today

        return {
            "daily_budget_tokens": self.daily_budget_tokens,
            "used_today_tokens": used_today,
            "remaining_tokens": remaining,
            "percentage_used": (used_today / self.daily_budget_tokens * 100),
            "over_budget": used_today > self.daily_budget_tokens,
        }

    def can_spawn_agent(self, estimated_tokens: int | None = None) -> tuple[bool, str | None]:
        """Check if we can spawn an agent based on token budget.

        Args:
            estimated_tokens: Optional estimated token usage for the agent run

        Returns:
            Tuple of (can_spawn, reason if not)
        """
        # Unlimited budget (0 or negative means no limit)
        if self.daily_budget_tokens <= 0:
            return True, None

        status = self.get_budget_status()

        # Already over budget
        if status["over_budget"]:
            return (
                False,
                f"Daily token budget exceeded: {_format_tokens(status['used_today_tokens'])} used "
                f"of {_format_tokens(self.daily_budget_tokens)}",
            )

        # Check if estimated tokens would exceed budget
        if estimated_tokens is not None:
            if status["used_today_tokens"] + estimated_tokens > self.daily_budget_tokens:
                return (
                    False,
                    f"Estimated {_format_tokens(estimated_tokens)} tokens would exceed budget. "
                    f"Remaining: {_format_tokens(status['remaining_tokens'])}",
                )

        return True, None
