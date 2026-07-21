"""Mandatory managed-datastore readiness checks for daemon startup."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from gobby.cli.services import is_qdrant_healthy
from gobby.memory.falkor_client import FalkorClient

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner


class ManagedServiceReadinessError(RuntimeError):
    """Raised when a required managed datastore is unavailable at startup."""


async def require_managed_services_ready(runner: GobbyRunner) -> None:
    """Require PostgreSQL, Qdrant, and FalkorDB before the HTTP server binds."""
    try:
        postgres_row = await asyncio.to_thread(
            runner.database.fetchone,
            "SELECT 1 AS ready",
        )
    except Exception as exc:
        raise ManagedServiceReadinessError(
            f"Managed PostgreSQL readiness check failed: {exc}"
        ) from exc
    if postgres_row is None:
        raise ManagedServiceReadinessError("Managed PostgreSQL readiness check returned no result")

    qdrant_url = runner.config.databases.qdrant.url
    if not qdrant_url:
        raise ManagedServiceReadinessError("Qdrant configuration is missing; run `gobby install`")
    if not await is_qdrant_healthy(qdrant_url):
        raise ManagedServiceReadinessError(f"Qdrant is not healthy at {qdrant_url}")

    falkor = runner.config.databases.falkordb
    if not falkor.password:
        raise ManagedServiceReadinessError("FalkorDB credentials are missing; run `gobby install`")
    client = FalkorClient(
        host=falkor.host,
        port=falkor.port,
        password=falkor.password,
        graph_name=falkor.graph_name,
        timeout=5.0,
    )
    try:
        if not await client.ping():
            raise ManagedServiceReadinessError(
                f"FalkorDB authentication or PING failed at {falkor.host}:{falkor.port}"
            )
    finally:
        await client.close()
