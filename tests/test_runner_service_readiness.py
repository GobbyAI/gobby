from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import httpx
import psycopg
import pytest

from gobby import runner_service_readiness as readiness
from gobby.memory.falkor_client import FalkorConnectionError

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

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
    def __init__(
        self,
        *ping_results: bool | Exception,
        close_error: Exception | None = None,
    ) -> None:
        self.ping_results = ping_results
        self.close_error = close_error
        self.ping_calls = 0
        self.close_calls = 0

    async def ping(self) -> bool:
        result = self.ping_results[min(self.ping_calls, len(self.ping_results) - 1)]
        self.ping_calls += 1
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _client_factory(client: FakeFalkorClient) -> Any:
    return lambda **_kwargs: client


def _config_runtime(config: SimpleNamespace) -> SimpleNamespace:
    """Stub the runtime capture chain readiness reads its config through."""
    return SimpleNamespace(capture=lambda: SimpleNamespace(snapshot=SimpleNamespace(active=config)))


def _runner(*, qdrant_url: str | None, falkor_password: str | None) -> GobbyRunner:
    config = SimpleNamespace(
        test_mode=False,
        databases=SimpleNamespace(
            qdrant=SimpleNamespace(url=qdrant_url),
            falkordb=SimpleNamespace(
                host="127.0.0.1",
                port=16379,
                password=falkor_password,
                graph_name="gobby_kg",
            ),
        ),
    )
    return cast(
        "GobbyRunner",
        SimpleNamespace(
            database=SimpleNamespace(fetchone=lambda _sql: {"ready": 1}),
            config_runtime=_config_runtime(config),
        ),
    )


@pytest.fixture(autouse=True)
def disable_readiness_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "MANAGED_SERVICE_READINESS_TIMEOUT_SECONDS", 0.0)


@pytest.mark.asyncio
async def test_unavailable_postgres_blocks_startup() -> None:
    runner = _runner(qdrant_url="http://localhost:6333", falkor_password="secret")

    def _fail(_sql: str) -> dict[str, int]:
        raise psycopg.OperationalError("connection refused")

    database: Any = runner.database
    database.fetchone = _fail

    with pytest.raises(readiness.ManagedServiceReadinessError, match="PostgreSQL readiness"):
        await readiness.require_managed_services_ready(runner)


@pytest.mark.asyncio
async def test_unexpected_postgres_error_propagates() -> None:
    runner = _runner(qdrant_url="http://localhost:6333", falkor_password="secret")

    def _fail(_sql: str) -> dict[str, int]:
        raise RuntimeError("programming error")

    database: Any = runner.database
    database.fetchone = _fail

    with pytest.raises(RuntimeError, match="programming error"):
        await readiness.require_managed_services_ready(runner)


@pytest.mark.asyncio
async def test_missing_qdrant_configuration_blocks_startup() -> None:
    with pytest.raises(readiness.ManagedServiceReadinessError, match="Qdrant configuration"):
        await readiness.require_managed_services_ready(
            _runner(qdrant_url=None, falkor_password="secret")
        )


async def test_test_mode_skips_managed_service_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_services = object()
    config = SimpleNamespace(test_mode=True, databases=managed_services)
    check = AsyncMock()
    monkeypatch.setattr(readiness, "_check_managed_services_ready_once", check)
    runner = cast(
        "GobbyRunner",
        SimpleNamespace(config_runtime=_config_runtime(config)),
    )

    await readiness.require_managed_services_ready(runner)

    assert check.await_count == 0
    assert config.test_mode is True
    assert config.databases is managed_services


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
    assert health.calls == 0


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
async def test_falkordb_ping_exception_is_wrapped_and_client_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health = FakeHealthCheck(True)
    client = FakeFalkorClient(
        FalkorConnectionError("connection reset"),
        close_error=RuntimeError("cleanup failed"),
    )
    monkeypatch.setattr(readiness, "is_qdrant_healthy", health)
    monkeypatch.setattr(readiness, "FalkorClient", _client_factory(client))

    with pytest.raises(
        readiness.ManagedServiceReadinessError,
        match="FalkorDB readiness check failed at 127.0.0.1:16379",
    ) as exc_info:
        await readiness.require_managed_services_ready(
            _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
        )

    assert client.close_calls == 1
    assert exc_info.value.__notes__ == ["FalkorDB readiness client cleanup failed: cleanup failed"]


@pytest.mark.asyncio
async def test_falkordb_cleanup_error_propagates_without_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeFalkorClient(True, close_error=RuntimeError("cleanup failed"))
    monkeypatch.setattr(readiness, "is_qdrant_healthy", FakeHealthCheck(True))
    monkeypatch.setattr(readiness, "FalkorClient", _client_factory(client))

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await readiness.require_managed_services_ready(
            _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
        )

    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_qdrant_http_error_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_qdrant(_url: str) -> bool:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(readiness, "is_qdrant_healthy", fail_qdrant)

    with pytest.raises(
        readiness.ManagedServiceReadinessError,
        match="Qdrant readiness check failed",
    ):
        await readiness.require_managed_services_ready(
            _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
        )


@pytest.mark.asyncio
async def test_unexpected_qdrant_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_qdrant(_url: str) -> bool:
        raise RuntimeError("programming error")

    monkeypatch.setattr(readiness, "is_qdrant_healthy", fail_qdrant)

    with pytest.raises(RuntimeError, match="programming error"):
        await readiness.require_managed_services_ready(
            _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
        )


@pytest.mark.asyncio
async def test_unexpected_falkordb_error_propagates_and_client_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeFalkorClient(RuntimeError("programming error"))
    monkeypatch.setattr(readiness, "is_qdrant_healthy", FakeHealthCheck(True))
    monkeypatch.setattr(readiness, "FalkorClient", _client_factory(client))

    with pytest.raises(RuntimeError, match="programming error"):
        await readiness.require_managed_services_ready(
            _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
        )

    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_healthy_managed_stack_passes_startup_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health = FakeHealthCheck(True)
    client = FakeFalkorClient(True)
    monkeypatch.setattr(readiness, "is_qdrant_healthy", health)
    monkeypatch.setattr(readiness, "FalkorClient", _client_factory(client))

    await readiness.require_managed_services_ready(
        _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
    )

    assert health.calls == 1
    assert client.ping_calls == 1
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_transient_runtime_readiness_failure_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health = FakeHealthCheck(False, True)
    client = FakeFalkorClient(True)
    monkeypatch.setattr(readiness, "MANAGED_SERVICE_READINESS_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(readiness, "MANAGED_SERVICE_READINESS_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(readiness, "is_qdrant_healthy", health)
    monkeypatch.setattr(readiness, "FalkorClient", _client_factory(client))

    await readiness.require_managed_services_ready(
        _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
    )

    assert health.calls == 2
    assert client.ping_calls == 1
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_later_falkordb_degradation_does_not_request_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
    runner._shutdown_requested = False
    qdrant_health = FakeHealthCheck(True, True)
    client = FakeFalkorClient(True, False)
    monkeypatch.setattr(readiness, "is_qdrant_healthy", qdrant_health)
    monkeypatch.setattr(readiness, "FalkorClient", _client_factory(client))

    await readiness.require_managed_services_ready(runner)
    with pytest.raises(readiness.ManagedServiceReadinessError, match="authentication or PING"):
        await readiness.require_managed_services_ready(runner)

    assert qdrant_health.calls == 2
    assert client.ping_calls == 2
    assert client.close_calls == 2
    assert runner._shutdown_requested is False


@pytest.mark.asyncio
async def test_later_qdrant_degradation_does_not_request_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(qdrant_url="http://localhost:6333", falkor_password="secret")
    runner._shutdown_requested = False
    qdrant_health = FakeHealthCheck(True, False)
    client = FakeFalkorClient(True)
    monkeypatch.setattr(readiness, "is_qdrant_healthy", qdrant_health)
    monkeypatch.setattr(readiness, "FalkorClient", _client_factory(client))

    await readiness.require_managed_services_ready(runner)
    with pytest.raises(readiness.ManagedServiceReadinessError, match="Qdrant is not healthy"):
        await readiness.require_managed_services_ready(runner)

    assert qdrant_health.calls == 2
    assert client.ping_calls == 1
    assert client.close_calls == 1
    assert runner._shutdown_requested is False
