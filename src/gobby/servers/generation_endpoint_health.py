"""Daemon-owned health snapshots for configured generation endpoints."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from gobby.config.ai import GenerationEndpointConfig
from gobby.servers.local_provider_models import (
    generation_endpoint_probe_result,
    probe_generation_endpoints,
)

logger = logging.getLogger(__name__)

GENERATION_ENDPOINT_REFRESH_INTERVAL_SECONDS = 60.0

type EndpointResolver = Callable[[], Mapping[str, GenerationEndpointConfig]]
type EndpointProbe = Callable[
    [dict[str, GenerationEndpointConfig]], Awaitable[list[dict[str, Any]]]
]
type EndpointConfiguration = tuple[tuple[str, str, str, str, str, str | None], ...]


class GenerationEndpointHealthCoordinator:
    """Refresh generation health in one daemon-owned background loop."""

    def __init__(
        self,
        endpoint_resolver: EndpointResolver,
        *,
        probe: EndpointProbe = probe_generation_endpoints,
        interval_seconds: float = GENERATION_ENDPOINT_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Generation endpoint refresh interval must be positive")
        self._endpoint_resolver = endpoint_resolver
        self._probe = probe
        self._interval_seconds = interval_seconds
        self._wake = asyncio.Event()
        self._endpoints: dict[str, GenerationEndpointConfig] = {}
        self._endpoint_signatures: dict[str, tuple[str, str, str, str, str | None]] = {}
        self._configuration: EndpointConfiguration = ()
        self._revision = 0
        self._rows: list[dict[str, Any]] = []
        self._sync_configuration()

    def snapshot(self) -> list[dict[str, Any]]:
        """Return defensive copies of the latest ordered health rows."""
        return [dict(row) for row in self._rows]

    def configuration_changed(self) -> None:
        """Publish pending rows and wake the loop when endpoint settings changed."""
        if self._sync_configuration():
            self._wake.set()

    async def run(self, shutdown_requested: Callable[[], bool]) -> None:
        """Refresh immediately and every 60 seconds, coalescing wake-ups."""
        while not shutdown_requested():
            self._wake.clear()
            self._sync_configuration()
            await self._refresh()
            if shutdown_requested():
                return
            if self._wake.is_set():
                continue
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                pass

    async def _refresh(self) -> None:
        endpoints = dict(self._endpoints)
        revision = self._revision
        try:
            results = await self._probe(endpoints)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc).strip() or type(exc).__name__
            results = [
                self._failed_result(name, endpoint, error) for name, endpoint in endpoints.items()
            ]

        if revision != self._revision:
            return

        by_name = {
            name: dict(result)
            for result in results
            if isinstance((name := result.get("name")), str)
        }
        self._rows = [
            by_name.get(
                name,
                self._failed_result(name, endpoint, "probe returned no result"),
            )
            for name, endpoint in endpoints.items()
        ]

    def _sync_configuration(self) -> bool:
        try:
            endpoints = dict(self._endpoint_resolver())
        except Exception:
            logger.warning(
                "Failed to resolve generation endpoints for health refresh", exc_info=True
            )
            return False

        signatures = {
            name: self._endpoint_signature(endpoint) for name, endpoint in endpoints.items()
        }
        configuration: EndpointConfiguration = tuple(
            (name, *signatures[name]) for name in endpoints
        )
        if configuration == self._configuration:
            return False

        existing = {str(row.get("name")): row for row in self._rows}
        rows: list[dict[str, Any]] = []
        for name, endpoint in endpoints.items():
            if self._endpoint_signatures.get(name) == signatures[name] and name in existing:
                rows.append(dict(existing[name]))
            else:
                rows.append(self._failed_result(name, endpoint, "probe pending"))

        self._endpoints = endpoints
        self._endpoint_signatures = signatures
        self._configuration = configuration
        self._revision += 1
        self._rows = rows
        return True

    @staticmethod
    def _endpoint_signature(
        endpoint: GenerationEndpointConfig,
    ) -> tuple[str, str, str, str, str | None]:
        return (
            endpoint.protocol,
            endpoint.wire_api,
            endpoint.api_base,
            endpoint.model,
            endpoint.api_key,
        )

    @staticmethod
    def _failed_result(
        name: str,
        endpoint: GenerationEndpointConfig,
        error: str,
    ) -> dict[str, Any]:
        result = generation_endpoint_probe_result(name, endpoint)
        result["error"] = error
        return result
