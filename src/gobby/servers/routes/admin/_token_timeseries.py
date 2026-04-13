"""Token time-series endpoint — hourly buckets of spent and saved tokens."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


def register_token_timeseries_routes(router: APIRouter, server: HTTPServer) -> None:
    @router.get("/tokens/timeseries")
    async def get_token_timeseries(
        hours: int = Query(24, ge=0, le=8760),
        project_id: str | None = Query(None),
    ) -> dict[str, Any]:
        """Return hourly buckets of tokens spent and tokens saved.

        Args:
            hours: Time window in hours. 0 = all time.
            project_id: Filter to a specific project.
        """
        db: DatabaseProtocol = server.services.database

        clauses: list[str] = []
        params: list[str] = []

        if hours > 0:
            clauses.append("AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)")
            params.append(f"-{hours} hours")

        if project_id:
            clauses.append("AND project_id = ?")
            params.append(project_id)

        where = " ".join(clauses)

        # --- tokens spent (from sessions) ---
        spent_by_bucket: dict[str, int] = {}
        try:
            rows = db.fetchall(
                "SELECT strftime('%Y-%m-%dT%H:00:00', created_at) as bucket, "
                "  COALESCE(SUM(usage_input_tokens + usage_output_tokens), 0) as tokens_spent "
                f"FROM sessions WHERE 1=1 {where} "
                "GROUP BY bucket",
                tuple(params),
            )
            for r in rows:
                spent_by_bucket[r["bucket"]] = r["tokens_spent"]
        except sqlite3.Error as e:
            logger.warning(f"Failed to query tokens spent: {e}")

        # --- tokens saved (from savings_ledger) ---
        saved_by_bucket: dict[str, int] = {}
        try:
            rows = db.fetchall(
                "SELECT strftime('%Y-%m-%dT%H:00:00', created_at) as bucket, "
                "  COALESCE(SUM(tokens_saved), 0) as tokens_saved "
                f"FROM savings_ledger WHERE 1=1 {where} "
                "GROUP BY bucket",
                tuple(params),
            )
            for r in rows:
                saved_by_bucket[r["bucket"]] = r["tokens_saved"]
        except sqlite3.Error as e:
            logger.warning(f"Failed to query tokens saved: {e}")

        # --- merge into sorted buckets ---
        all_timestamps = sorted(set(spent_by_bucket) | set(saved_by_bucket))
        buckets = [
            {
                "timestamp": ts,
                "tokens_spent": spent_by_bucket.get(ts, 0),
                "tokens_saved": saved_by_bucket.get(ts, 0),
            }
            for ts in all_timestamps
        ]

        return {
            "hours": hours,
            "buckets": buckets,
        }
