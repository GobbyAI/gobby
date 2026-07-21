from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gobby import runner_service_readiness as readiness

pytestmark = pytest.mark.unit


class FakeHealthCheck:
    def __init__(self, *results: bool) -> None:
        self.results = results
        self.calls = 0

    async def __call__(self, _url: str) -> bool:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


class FakeFalkorClient:
    def __init__(self, *ping_results: bool) -> None:
        self.ping_results = ping_results
        self.ping_calls = 0
        self.close_calls = 0

    async def ping(self) -> bool:
        result = self.ping_results[min(self.ping_calls, len(self.ping_results) - 1)]
        self.ping_calls += 1
        return result

    async def close(self) -> None:
        self.close_calls += 1


def _client_factory(client: FakeFalkorClient) -> Any:
    return lambda **_kwargs: client


def _runner(*, qdrant_url: str | None, falkor_password: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        database=SimpleNamespace(fetchone=lambda _sql: {"ready": 1}),
        config=SimpleNamespace(
            databases=SimpleNamespace(
                qdrant=SimpleNamespace(url=qdrant_url),
                falkordb=SimpleNamespace(
                    host="127.0.0.1",
                    port=16379,
                    password=falkor_password,
                    graph_name="gobby_kg",
                ),
            )
        ),
    )


@pytest.mark.asyncio
async def test_unavailable_postgres_blocks_startup() -> None:
    runner = _runner(qdrant_url="http://localhost:6333", falkor_password="secret")

    def _fail(_sql: str) -> dict[str, int]:
        raise RuntimeError("connection refused")

    runner.database.fetchone = _fail

    with pytest.raises(readiness.ManagedServiceReadinessError, match="PostgreSQL readiness"):
        await readiness.require_managed_services_ready(runner)


@pytest.mark.asyncio
async def test_missing_qdrant_configuration_blocks_startup() -> None:
    with pytest.raises(readiness.ManagedServiceReadinessError, match="Qdrant configuration"):
        await readiness.require_managed_services_ready(
            _runner(qdrant_url=None, falkor_password="secret")
        )


@pytest.mark.asyncio
async def test_unhealthy_qdrant_blocks_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    health = FakeHealthCheck(False)
    monkeypatch.setattr(readiness, "is_qdrant_healthy", health)

    with pytest.raises(readiness.ManagedServiceReadinessError, match="Qdrant is not healthy"):
        await readiness.require_managed_services_ready(
            _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
        )
    assert health.calls == 1


@pytest.mark.asyncio
async def test_missing_falkordb_credentials_block_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health = FakeHealthCheck(True)
    monkeypatch.setattr(readiness, "is_qdrant_healthy", health)

    with pytest.raises(readiness.ManagedServiceReadinessError, match="credentials are missing"):
        await readiness.require_managed_services_ready(
            _runner(qdrant_url="http://localhost:6333", falkor_password=None)
        )
    assert health.calls == 1


@pytest.mark.asyncio
async def test_falkordb_authentication_failure_blocks_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health = FakeHealthCheck(True)
    client = FakeFalkorClient(False)
    monkeypatch.setattr(readiness, "is_qdrant_healthy", health)
    monkeypatch.setattr(readiness, "FalkorClient", _client_factory(client))

    with pytest.raises(readiness.ManagedServiceReadinessError, match="authentication or PING"):
        await readiness.require_managed_services_ready(
            _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
        )

    assert health.calls == 1
    assert client.ping_calls == 1
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_healthy_managed_stack_passes_startup_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health = FakeHealthCheck(True)
    client = FakeFalkorClient(True)
    monkeypatch.setattr(readiness, "is_qdrant_healthy", health)
    monkeypatch.setattr(readiness, "FalkorClient", _client_factory(client))

    result = await readiness.require_managed_services_ready(
        _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
    )

    assert result is None
    assert health.calls == 1
    assert client.ping_calls == 1
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_later_qdrant_or_falkordb_degradation_does_not_request_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
    runner._shutdown_requested = False
    qdrant_health = FakeHealthCheck(True, False)
    client = FakeFalkorClient(True, False)
    monkeypatch.setattr(readiness, "is_qdrant_healthy", qdrant_health)
    monkeypatch.setattr(readiness, "FalkorClient", _client_factory(client))

    await readiness.require_managed_services_ready(runner)

    assert qdrant_health.calls == 1
    assert client.ping_calls == 1
    assert client.close_calls == 1
    assert runner._shutdown_requested is False
