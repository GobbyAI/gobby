"""Token usage aggregation endpoint."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query

from gobby.storage.token_events import TokenEventStore

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def register_usage_routes(router: APIRouter, server: HTTPServer) -> None:
    @router.get("/usage")
    async def get_usage(
        hours: int = Query(0, ge=0, le=8760),
        project_id: str | None = Query(None),
    ) -> dict[str, Any]:
        """Aggregate token usage from token_events."""
        try:
            store = TokenEventStore(server.services.database)
            breakdown = store.get_breakdown(hours=hours, project_id=project_id)
        except Exception as exc:
            logger.warning("Failed to load token usage breakdown: %s", exc)
            breakdown = {
                "totals": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "session_count": 0,
                },
                "by_source": {},
                "by_model": {},
            }
        return {
            "hours": hours,
            "totals": breakdown["totals"],
            "by_source": breakdown["by_source"],
            "by_model": breakdown["by_model"],
        }
