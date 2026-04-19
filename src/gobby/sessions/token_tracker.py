"""Token usage aggregation helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from gobby.storage.token_events import TokenEventStore

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol


class SessionTokenTracker:
    """Aggregate token usage from token_events, with a legacy session fallback for tests."""

    def __init__(
        self,
        *,
        db: DatabaseProtocol | None = None,
        session_storage: Any | None = None,
    ) -> None:
        self.db = db
        self.session_storage = session_storage

    def get_usage_summary(self, days: int = 1, project_id: str | None = None) -> dict[str, Any]:
        """Get usage summary for the specified number of days."""
        if self.db is not None:
            store = TokenEventStore(self.db)
            breakdown = store.get_breakdown(days=days, project_id=project_id)
            totals = breakdown["totals"]
            return {
                "total_input_tokens": totals["input_tokens"],
                "total_output_tokens": totals["output_tokens"],
                "total_cache_creation_tokens": totals["cache_creation_tokens"],
                "total_cache_read_tokens": totals["cache_read_tokens"],
                "session_count": totals["session_count"],
                "usage_by_model": breakdown["by_model"],
                "usage_by_source": breakdown["by_source"],
                "period_days": days,
            }

        if self.session_storage is None:
            raise ValueError("SessionTokenTracker requires db or session_storage")

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

            model = session.model or "unknown"
            usage_by_model.setdefault(
                model,
                {"input_tokens": 0, "output_tokens": 0, "sessions": 0},
            )
            usage_by_model[model]["input_tokens"] += inp
            usage_by_model[model]["output_tokens"] += out
            usage_by_model[model]["sessions"] += 1

            source = getattr(session, "source", None) or "unknown"
            usage_by_source.setdefault(
                source,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                    "sessions": 0,
                },
            )
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
