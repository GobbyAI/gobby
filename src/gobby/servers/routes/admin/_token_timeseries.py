"""Token time-series endpoint — event-time buckets of spent and saved tokens."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Query

from gobby.storage.token_events import (
    VALID_GRANULARITIES,
    TimeSeriesGranularity,
    TokenEventStore,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


def _coerce_granularity(value: str) -> TimeSeriesGranularity:
    if value not in VALID_GRANULARITIES:
        raise ValueError(f"Unsupported granularity: {value}")
    return cast(TimeSeriesGranularity, value)


def _bucket_expression(column: str, granularity: TimeSeriesGranularity) -> str:
    if granularity == "30m":
        return (
            "CASE "
            f"WHEN CAST(strftime('%M', {column}) AS INTEGER) < 30 "
            f"THEN strftime('%Y-%m-%dT%H:00:00Z', {column}) "
            f"ELSE strftime('%Y-%m-%dT%H:30:00Z', {column}) "
            "END"
        )
    if granularity == "1d":
        return f"strftime('%Y-%m-%dT00:00:00Z', {column})"
    return f"strftime('%Y-%m-%dT%H:00:00Z', {column})"


def register_token_timeseries_routes(router: APIRouter, server: HTTPServer) -> None:
    @router.get("/tokens/timeseries")
    async def get_token_timeseries(
        hours: int = Query(24, ge=0, le=8760),
        project_id: str | None = Query(None),
        granularity: str = Query("1h", pattern="^(30m|1h|1d)$"),
    ) -> dict[str, Any]:
        """Return event-time buckets of tokens spent and tokens saved."""
        db = server.services.database
        store = TokenEventStore(db)
        bucket_granularity = _coerce_granularity(granularity)
        spent_rows = await server.run_db(
            store.get_timeseries,
            hours=hours,
            project_id=project_id,
            granularity=bucket_granularity,
        )
        spent_by_bucket = {row["timestamp"]: row["tokens_spent"] for row in spent_rows}

        clauses: list[str] = []
        params: list[Any] = []
        if hours > 0:
            clauses.append("AND datetime(created_at) >= datetime('now', ?)")
            params.append(f"-{hours} hours")
        if project_id:
            clauses.append("AND project_id = ?")
            params.append(project_id)

        where = " ".join(clauses)
        # _bucket_expression() is safe to interpolate here because FastAPI validates
        # granularity against ^(30m|1h|1d)$ before this query reaches db.fetchall().
        bucket_expr = _bucket_expression("created_at", bucket_granularity)
        rows = await server.run_db(
            db.fetchall,
            f"""
            SELECT
                {bucket_expr} AS bucket,
                COALESCE(SUM(tokens_saved), 0) AS tokens_saved
            FROM savings_ledger
            WHERE 1=1 {where}
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            tuple(params),
        )
        saved_by_bucket = {
            str(row["bucket"]): int(row["tokens_saved"] or 0)
            for row in rows
            if row["bucket"] is not None
        }

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
            "granularity": bucket_granularity,
            "buckets": buckets,
        }
