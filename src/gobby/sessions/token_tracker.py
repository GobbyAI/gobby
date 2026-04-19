"""Session-based token usage aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from gobby.storage.session_models import Session


class SessionStorageProtocol(Protocol):
    """Protocol for the session storage dependency used by usage aggregation."""

    def get_sessions_since(
        self, since: datetime, project_id: str | None = None
    ) -> list[Session]: ...


@dataclass
class SessionTokenTracker:
    """Aggregate token usage from sessions over time.

    Example:
        tracker = SessionTokenTracker(session_storage=session_manager)

        # Get usage summary for last 7 days
        summary = tracker.get_usage_summary(days=7)
    """

    session_storage: SessionStorageProtocol

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
