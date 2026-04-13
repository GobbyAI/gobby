"""Token usage aggregation endpoint."""

import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def register_usage_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.get("/usage")
    async def get_usage(
        hours: int = Query(0, ge=0, le=8760),
        project_id: str | None = Query(None),
    ) -> dict[str, Any]:
        """Aggregate token usage from sessions table.

        Args:
            hours: Time window in hours. 0 = all time.
            project_id: Filter to a specific project.
        """
        db = server.services.database

        clauses: list[str] = []
        params: list[str] = []

        if hours > 0:
            clauses.append("AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)")
            params.append(f"-{hours} hours")

        if project_id:
            clauses.append("AND project_id = ?")
            params.append(project_id)

        where = " ".join(clauses)

        # Totals
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "session_count": 0,
        }
        try:
            rows = db.fetchall(
                "SELECT "
                "  COALESCE(SUM(usage_input_tokens), 0) as input_tokens, "
                "  COALESCE(SUM(usage_output_tokens), 0) as output_tokens, "
                "  COALESCE(SUM(usage_cache_read_tokens), 0) as cache_read_tokens, "
                "  COALESCE(SUM(usage_cache_creation_tokens), 0) as cache_creation_tokens, "
                "  COUNT(*) as session_count "
                f"FROM sessions WHERE 1=1 {where}",
                tuple(params),
            )
            if rows:
                r = rows[0]
                totals = {
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cache_read_tokens": r["cache_read_tokens"],
                    "cache_creation_tokens": r["cache_creation_tokens"],
                    "session_count": r["session_count"],
                }
        except sqlite3.Error as e:
            logger.warning(f"Failed to get usage totals: {e}")

        # By source
        by_source: dict[str, dict[str, Any]] = {}
        try:
            rows = db.fetchall(
                "SELECT source, "
                "  COALESCE(SUM(usage_input_tokens), 0) as input_tokens, "
                "  COALESCE(SUM(usage_output_tokens), 0) as output_tokens, "
                "  COALESCE(SUM(usage_cache_read_tokens), 0) as cache_read_tokens, "
                "  COALESCE(SUM(usage_cache_creation_tokens), 0) as cache_creation_tokens, "
                "  COUNT(*) as session_count "
                f"FROM sessions WHERE 1=1 {where} "
                "GROUP BY source",
                tuple(params),
            )
            for r in rows:
                src = r["source"] or "unknown"
                by_source[src] = {
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cache_read_tokens": r["cache_read_tokens"],
                    "cache_creation_tokens": r["cache_creation_tokens"],
                    "session_count": r["session_count"],
                }
        except sqlite3.Error as e:
            logger.warning(f"Failed to get usage by source: {e}")

        # By model
        by_model: dict[str, dict[str, Any]] = {}
        try:
            rows = db.fetchall(
                "SELECT model, "
                "  COALESCE(SUM(usage_input_tokens), 0) as input_tokens, "
                "  COALESCE(SUM(usage_output_tokens), 0) as output_tokens, "
                "  COALESCE(SUM(usage_cache_read_tokens), 0) as cache_read_tokens, "
                "  COALESCE(SUM(usage_cache_creation_tokens), 0) as cache_creation_tokens, "
                "  COUNT(*) as session_count "
                f"FROM sessions WHERE 1=1 {where} "
                "GROUP BY model "
                "ORDER BY input_tokens + output_tokens DESC",
                tuple(params),
            )
            for r in rows:
                mdl = r["model"] or "unknown"
                by_model[mdl] = {
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cache_read_tokens": r["cache_read_tokens"],
                    "cache_creation_tokens": r["cache_creation_tokens"],
                    "session_count": r["session_count"],
                }
        except sqlite3.Error as e:
            logger.warning(f"Failed to get usage by model: {e}")

        return {
            "hours": hours,
            "totals": totals,
            "by_source": by_source,
            "by_model": by_model,
        }
