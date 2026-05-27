"""SavingsTracker — records and summarizes token savings."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from gobby.storage.sql_dialect import newer_than_now_expr

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

# Empirical chars-per-token for code-heavy content
CHARS_PER_TOKEN = 3.7

# Only these categories produce real, measurable savings.
VALID_CATEGORIES: frozenset[str] = frozenset({"code_index", "discovery", "compression"})


class SavingsTracker:
    """Track token savings from Gobby features.

    Savings categories:
    - code_index: symbol retrieval vs full file read
    - discovery: progressive schema loading
    - compression: gsqz output compression (Bash tool results only)
    """

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def record(
        self,
        category: str,
        original_chars: int,
        actual_chars: int,
        session_id: str | None = None,
        project_id: str | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record a savings event using character counts (converted to tokens)."""
        if category not in VALID_CATEGORIES:
            logger.warning(f"Rejected savings record for invalid category {category!r}")
            return
        original_tokens = max(0, int(original_chars / CHARS_PER_TOKEN))
        actual_tokens = max(0, int(actual_chars / CHARS_PER_TOKEN))
        self.record_tokens(
            category=category,
            original_tokens=original_tokens,
            actual_tokens=actual_tokens,
            session_id=session_id,
            project_id=project_id,
            model=model,
            metadata=metadata,
        )

    def record_tokens(
        self,
        category: str,
        original_tokens: int,
        actual_tokens: int,
        session_id: str | None = None,
        project_id: str | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record a savings event using token counts."""
        if category not in VALID_CATEGORIES:
            logger.warning(f"Rejected savings record for invalid category {category!r}")
            return
        tokens_saved = max(0, original_tokens - actual_tokens)

        self.db.execute(
            "INSERT INTO savings_ledger "
            "(session_id, project_id, category, original_tokens, actual_tokens, "
            "tokens_saved, model, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                project_id,
                category,
                original_tokens,
                actual_tokens,
                tokens_saved,
                model,
                json.dumps(metadata) if metadata else None,
            ),
        )

    def get_summary(self, days: int = 1, project_id: str | None = None) -> dict[str, Any]:
        """Get savings summary for the specified time window."""
        params: list[Any] = [days]

        project_filter = ""
        if project_id:
            project_filter = "AND project_id = %s"
            params.append(project_id)

        # Build category IN clause
        cat_placeholders = ", ".join("%s" for _ in VALID_CATEGORIES)
        cat_filter = f"AND category IN ({cat_placeholders})"
        cat_params = list(VALID_CATEGORIES)

        window_sql = newer_than_now_expr(self.db, "created_at", "%s", "day")
        rows = self.db.fetchall(
            f"SELECT category, "
            f"SUM(original_tokens) as original_tokens, "
            f"SUM(actual_tokens) as actual_tokens, "
            f"SUM(tokens_saved) as tokens_saved, "
            f"COUNT(*) as event_count "
            f"FROM savings_ledger "
            f"WHERE {window_sql} {project_filter} {cat_filter} "
            f"GROUP BY category",
            tuple(params + cat_params),
        )

        categories: dict[str, Any] = {}
        total_tokens_saved = 0
        total_events = 0

        for row in rows:
            cat = row["category"]
            categories[cat] = {
                "original_tokens": row["original_tokens"] or 0,
                "actual_tokens": row["actual_tokens"] or 0,
                "tokens_saved": row["tokens_saved"] or 0,
                "event_count": row["event_count"] or 0,
            }
            total_tokens_saved += row["tokens_saved"] or 0
            total_events += row["event_count"] or 0

        return {
            "days": days,
            "total_tokens_saved": total_tokens_saved,
            "total_events": total_events,
            "categories": categories,
        }

    def get_cumulative(self, days: int = 30, project_id: str | None = None) -> dict[str, Any]:
        """Get cumulative savings over a longer window (for dashboard headline)."""
        return self.get_summary(days=days, project_id=project_id)
