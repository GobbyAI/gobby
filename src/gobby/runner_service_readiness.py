"""Mandatory managed-datastore readiness checks for daemon startup."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import psycopg

from gobby.cli.services import is_qdrant_healthy
from gobby.memory.falkor_client import FalkorClient, FalkorConnectionError, FalkorQueryError

if TYPE_CHECKING:
    from gobby.config.persistence import FalkorConfig
    from gobby.runner import GobbyRunner


class ManagedServiceReadinessError(RuntimeError):
    """Raised when a required managed datastore is unavailable at startup."""


MANAGED_SERVICE_READINESS_TIMEOUT_SECONDS = 30.0
MANAGED_SERVICE_READINESS_RETRY_DELAY_SECONDS = 1.0


async def _check_managed_services_ready_once(
    runner: GobbyRunner,
    *,
    qdrant_url: str,
    falkor_config: FalkorConfig,
) -> None:
    try:
        postgres_row = await asyncio.to_thread(
            runner.database.fetchone,
            "SELECT 1 AS ready",
        )
    except psycopg.Error as exc:
        raise ManagedServiceReadinessError(
            f"Managed PostgreSQL readiness check failed: {exc}"
        ) from exc
    if postgres_row is None:
        raise ManagedServiceReadinessError("Managed PostgreSQL readiness check returned no result")

    try:
        qdrant_healthy = await is_qdrant_healthy(qdrant_url)
    except httpx.HTTPError as exc:
        raise ManagedServiceReadinessError(
            f"Qdrant readiness check failed at {qdrant_url}: {exc}"
        ) from exc
    if not qdrant_healthy:
        raise ManagedServiceReadinessError(f"Qdrant is not healthy at {qdrant_url}")

    falkor = falkor_config
    client = FalkorClient(
        host=falkor.host,
        port=falkor.port,
        password=falkor.password,
        graph_name=falkor.graph_name,
        timeout=5.0,
    )
    primary_error: BaseException | None = None
    try:
        try:
            falkor_healthy = await client.ping()
        except (FalkorConnectionError, FalkorQueryError) as exc:
            raise ManagedServiceReadinessError(
                f"FalkorDB readiness check failed at {falkor.host}:{falkor.port}: {exc}"
            ) from exc
        if not falkor_healthy:
            raise ManagedServiceReadinessError(
                f"FalkorDB authentication or PING failed at {falkor.host}:{falkor.port}"
            )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            await client.close()
        except Exception as exc:
            if primary_error is None:
                raise
            primary_error.add_note(f"FalkorDB readiness client cleanup failed: {exc}")


async def require_managed_services_ready(runner: GobbyRunner) -> None:
    """Require PostgreSQL, Qdrant, and FalkorDB before the HTTP server binds."""
    config = runner.config_runtime.capture().snapshot.active
    if config.test_mode is True:
        return

    qdrant_url = config.databases.qdrant.url
    if not qdrant_url:
        raise ManagedServiceReadinessError("Qdrant configuration is missing; run `gobby install`")
    falkor = config.databases.falkordb
    if not falkor.password:
        raise ManagedServiceReadinessError("FalkorDB credentials are missing; run `gobby install`")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + MANAGED_SERVICE_READINESS_TIMEOUT_SECONDS
    while True:
        try:
            await _check_managed_services_ready_once(
                runner,
                qdrant_url=qdrant_url,
                falkor_config=falkor,
            )
            return
        except ManagedServiceReadinessError as exc:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise
            await asyncio.sleep(min(MANAGED_SERVICE_READINESS_RETRY_DELAY_SECONDS, remaining))
            if loop.time() >= deadline:
                raise exc
