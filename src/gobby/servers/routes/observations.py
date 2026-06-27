"""Debug routes for unmodeled transcript observations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query

from gobby.storage.unmodeled_observations import (
    COUNT_SEMANTICS,
    UnmodeledObservationStore,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


def create_observations_router(server: HTTPServer) -> APIRouter:
    router = APIRouter(prefix="/api/observations", tags=["observations"])
    store = UnmodeledObservationStore(server.services.database)

    @router.get("/unmodeled")
    async def list_unmodeled_observations(
        source: str | None = Query(None),
        kind: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ) -> dict[str, Any]:
        rows = await server.run_db(
            store.list_observations,
            source=source,
            kind=kind,
            limit=limit,
        )
        return {
            "count_semantics": COUNT_SEMANTICS,
            "observations": [row.__dict__ for row in rows],
        }

    return router
