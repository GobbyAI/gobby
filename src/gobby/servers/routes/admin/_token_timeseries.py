"""Token time-series endpoint — event-time buckets of spent tokens."""

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


def register_token_timeseries_routes(router: APIRouter, server: HTTPServer) -> None:
    @router.get("/tokens/timeseries")
    async def get_token_timeseries(
        hours: int = Query(24, ge=0, le=8760),
        project_id: str | None = Query(None),
        granularity: str = Query("1h", pattern="^(30m|1h|1d)$"),
    ) -> dict[str, Any]:
        """Return event-time buckets of tokens spent."""
        db = server.services.database
        store = TokenEventStore(db)
        bucket_granularity = _coerce_granularity(granularity)
        rows = await server.run_db(
            store.get_timeseries,
            hours=hours,
            project_id=project_id,
            granularity=bucket_granularity,
        )
        buckets = [
            {
                "timestamp": row["timestamp"],
                "tokens_spent": row["tokens_spent"],
            }
            for row in rows
        ]

        return {
            "hours": hours,
            "granularity": bucket_granularity,
            "buckets": buckets,
        }
