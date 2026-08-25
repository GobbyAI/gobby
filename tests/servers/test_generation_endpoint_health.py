from __future__ import annotations

import asyncio
import gzip
import threading
from collections.abc import Callable, Iterator
from contextlib import suppress
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from gobby.config.ai import GenerationEndpointConfig
from gobby.servers.generation_endpoint_health import GenerationEndpointHealthCoordinator
from gobby.servers.local_provider_models import (
    generation_endpoint_probe_result,
    probe_generation_endpoint,
    probe_generation_endpoints,
)

pytestmark = pytest.mark.unit


def _endpoint(
    *,
    model: str = "configured-model",
    api_base: str = "http://localhost:8000/v1",
    api_key: str | None = None,
) -> GenerationEndpointConfig:
    return GenerationEndpointConfig(
        protocol="openai-compatible",
        api_base=api_base,
        model=model,
        api_key=api_key,
    )


def _health_result(
    name: str,
    endpoint: GenerationEndpointConfig,
    *,
    healthy: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    result = generation_endpoint_probe_result(name, endpoint)
    result["healthy"] = healthy
    result["served_model"] = endpoint.model if healthy else None
    result["model_count"] = 1 if healthy else None
    result["error"] = error
    return result


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


class _FakeResponse:
    def __init__(self, url: str, payload: dict[str, Any], status_code: int = 200) -> None:
        self.request = httpx.Request("GET", url)
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code} error",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )


class _FakeSyncClient:
    def __init__(
        self,
        responses: dict[str, _FakeResponse],
        *,
        error: Exception | None = None,
    ) -> None:
        self.responses = responses
        self.error = error
        self.timeouts: list[float] = []

    def __enter__(self) -> _FakeSyncClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        timeout = kwargs.get("timeout")
        if isinstance(timeout, int | float):
            self.timeouts.append(float(timeout))
        if self.error is not None:
            raise self.error
        response = self.responses.get(url)
        if response is None:
            raise httpx.ConnectError("missing fixture", request=httpx.Request("GET", url))
        return response


def test_coordinator_seeds_ordered_pending_rows_and_returns_copies() -> None:
    endpoints = {
        "first": _endpoint(model="first-model"),
        "second": _endpoint(model="second-model", api_base="http://localhost:8001/v1"),
    }
    coordinator = GenerationEndpointHealthCoordinator(lambda: endpoints)

    snapshot = coordinator.snapshot()

    assert [row["name"] for row in snapshot] == ["first", "second"]
    assert all(row["healthy"] is False for row in snapshot)
    assert all(row["served_model"] is None for row in snapshot)
    assert all(row["model_count"] is None for row in snapshot)
    assert all(row["error"] == "probe pending" for row in snapshot)

    snapshot[0]["healthy"] = True
    snapshot.append({"name": "injected"})
    fresh = coordinator.snapshot()
    assert [row["name"] for row in fresh] == ["first", "second"]
    assert fresh[0]["healthy"] is False


@pytest.mark.asyncio
async def test_coordinator_refreshes_immediately_and_on_cadence() -> None:
    endpoints = {"remote": _endpoint()}
    calls = 0
    second_call = asyncio.Event()

    async def probe(
        current: dict[str, GenerationEndpointConfig],
    ) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 2:
            second_call.set()
        return [_health_result(name, endpoint) for name, endpoint in current.items()]

    coordinator = GenerationEndpointHealthCoordinator(
        lambda: endpoints,
        probe=probe,
        interval_seconds=0.01,
    )
    task = asyncio.create_task(coordinator.run(lambda: False))
    try:
        await asyncio.wait_for(second_call.wait(), timeout=1)
        assert calls == 2
        assert coordinator.snapshot()[0]["healthy"] is True
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_configuration_change_retains_removes_and_marks_pending_in_order() -> None:
    endpoints = {
        "first": _endpoint(model="first-model"),
        "removed": _endpoint(model="removed-model"),
    }
    calls: list[list[str]] = []

    async def probe(
        current: dict[str, GenerationEndpointConfig],
    ) -> list[dict[str, Any]]:
        calls.append(list(current))
        return [_health_result(name, endpoint) for name, endpoint in current.items()]

    coordinator = GenerationEndpointHealthCoordinator(
        lambda: endpoints,
        probe=probe,
        interval_seconds=60,
    )
    task = asyncio.create_task(coordinator.run(lambda: False))
    try:
        await _wait_until(lambda: coordinator.snapshot()[0]["healthy"] is True)

        endpoints = {
            "added": _endpoint(model="added-model", api_base="http://localhost:8002/v1"),
            "first": endpoints["first"],
        }
        coordinator.configuration_changed()

        pending = coordinator.snapshot()
        assert [row["name"] for row in pending] == ["added", "first"]
        assert pending[0]["error"] == "probe pending"
        assert pending[1]["healthy"] is True
        await _wait_until(
            lambda: len(calls) == 2 and all(row["healthy"] for row in coordinator.snapshot())
        )
        assert calls == [["first", "removed"], ["added", "first"]]
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_changed_endpoint_failure_replaces_prior_metadata() -> None:
    endpoints = {"remote": _endpoint(api_key="old-key")}
    calls = 0

    async def probe(
        current: dict[str, GenerationEndpointConfig],
    ) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        name, endpoint = next(iter(current.items()))
        if calls == 1:
            return [_health_result(name, endpoint)]
        return [_health_result(name, endpoint, healthy=False, error="connection refused")]

    coordinator = GenerationEndpointHealthCoordinator(
        lambda: endpoints,
        probe=probe,
        interval_seconds=60,
    )
    task = asyncio.create_task(coordinator.run(lambda: False))
    try:
        await _wait_until(lambda: coordinator.snapshot()[0]["healthy"] is True)

        endpoints = {"remote": _endpoint(api_key="new-key")}
        coordinator.configuration_changed()
        pending = coordinator.snapshot()[0]
        assert pending["error"] == "probe pending"
        assert pending["served_model"] is None
        assert pending["model_count"] is None

        await _wait_until(lambda: coordinator.snapshot()[0]["error"] == "connection refused")
        failed = coordinator.snapshot()[0]
        assert failed["healthy"] is False
        assert failed["served_model"] is None
        assert failed["model_count"] is None
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_configuration_wakeups_coalesce_and_stale_results_are_discarded() -> None:
    endpoints = {"remote": _endpoint(model="model-a")}
    calls: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    async def probe(
        current: dict[str, GenerationEndpointConfig],
    ) -> list[dict[str, Any]]:
        name, endpoint = next(iter(current.items()))
        calls.append(endpoint.model)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
            await release_second.wait()
        return [_health_result(name, endpoint)]

    coordinator = GenerationEndpointHealthCoordinator(
        lambda: endpoints,
        probe=probe,
        interval_seconds=60,
    )
    task = asyncio.create_task(coordinator.run(lambda: False))
    try:
        await asyncio.wait_for(first_started.wait(), timeout=1)
        for model in ("model-b", "model-c", "model-a"):
            endpoints = {"remote": _endpoint(model=model)}
            coordinator.configuration_changed()

        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=1)

        pending = coordinator.snapshot()[0]
        assert calls == ["model-a", "model-a"]
        assert pending["model"] == "model-a"
        assert pending["error"] == "probe pending"

        coordinator.configuration_changed()
        release_second.set()
        await _wait_until(lambda: coordinator.snapshot()[0]["healthy"] is True)
        assert calls == ["model-a", "model-a"]
        assert coordinator.snapshot()[0]["served_model"] == "model-a"
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_probe_generation_endpoint_runs_body_and_json_work_off_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
        api_base="https://models.example/v1",
        model="configured-model",
    )
    loop_thread = threading.get_ident()
    body_threads: list[int] = []
    json_threads: list[int] = []
    compressed = gzip.compress(b'{"data":[{"id":"served-model"}]}')

    class RecordingStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            body_threads.append(threading.get_ident())
            yield compressed

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=RecordingStream(),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    original_json = httpx.Response.json

    def recording_json(response: httpx.Response, **kwargs: Any) -> Any:
        json_threads.append(threading.get_ident())
        return original_json(response, **kwargs)

    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.Client", lambda **_kwargs: client
    )
    monkeypatch.setattr(httpx.Response, "json", recording_json)

    result = await probe_generation_endpoint("remote", endpoint)

    assert result["healthy"] is True
    assert result["served_model"] == "configured-model"
    assert result["model_count"] == 1
    assert body_threads and all(thread_id != loop_thread for thread_id in body_threads)
    assert json_threads and all(thread_id != loop_thread for thread_id in json_threads)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        pytest.param(
            _FakeResponse("http://localhost:1234/v1/models", {}, status_code=503),
            "503 error",
            id="http-error",
        ),
        pytest.param(
            MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(side_effect=ValueError("malformed models payload")),
            ),
            "malformed models payload",
            id="malformed-json",
        ),
    ],
)
async def test_probe_generation_endpoint_reports_response_errors(
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeResponse,
    expected_error: str,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="lmstudio",
        api_base="http://localhost:1234/v1",
        model="configured-model",
    )
    models_url = "http://localhost:1234/v1/models"
    client = _FakeSyncClient({models_url: response})
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.Client", lambda **_kwargs: client
    )

    result = await probe_generation_endpoint("studio", endpoint)

    assert result["healthy"] is False
    assert result["served_model"] is None
    assert result["model_count"] is None
    assert expected_error in result["error"]


@pytest.mark.asyncio
async def test_probe_generation_endpoint_applies_httpx_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="configured-model",
    )
    request = httpx.Request("GET", "http://localhost:8000/v1/models")
    client = _FakeSyncClient({}, error=httpx.ReadTimeout("worker timeout", request=request))
    constructor_timeouts: list[float] = []

    def client_factory(*, timeout: float) -> _FakeSyncClient:
        constructor_timeouts.append(timeout)
        return client

    monkeypatch.setattr("gobby.servers.local_provider_models.httpx.Client", client_factory)

    result = await probe_generation_endpoint("remote", endpoint, timeout=0.25)

    assert result["healthy"] is False
    assert result["error"] == "worker timeout"
    assert constructor_timeouts == [0.25]
    assert client.timeouts == [0.25]


@pytest.mark.asyncio
async def test_probe_generation_endpoint_caller_timeout_uses_independent_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="configured-model",
    )
    release = threading.Event()
    worker_done = threading.Event()
    response = _FakeResponse(
        "http://localhost:8000/v1/models",
        {"data": [{"id": "served-model"}]},
    )

    class BlockingClient(_FakeSyncClient):
        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            release.wait(timeout=1)
            return super().get(url, **kwargs)

        def __exit__(self, *_args: Any) -> None:
            worker_done.set()

    client = BlockingClient({"http://localhost:8000/v1/models": response})
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.Client", lambda **_kwargs: client
    )

    result = await probe_generation_endpoint("remote", endpoint, timeout=0.01)
    assert result["healthy"] is False
    assert result["error"] == "TimeoutError"

    release.set()
    assert await asyncio.to_thread(worker_done.wait, 1)
    assert result["healthy"] is False
    assert result["served_model"] is None
    assert result["model_count"] is None


@pytest.mark.asyncio
async def test_probe_generation_endpoints_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="configured-model",
    )

    async def fail_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(
        "gobby.servers.local_provider_models.probe_generation_endpoint",
        fail_probe,
    )

    results = await probe_generation_endpoints({"remote": endpoint})

    assert results == [
        {
            "name": "remote",
            "protocol": "openai-compatible",
            "provider_label": "OpenAI Compatible",
            "wire_api": "chat-completions",
            "api_base": "http://localhost:8000/v1",
            "model": "configured-model",
            "healthy": False,
            "served_model": None,
            "model_count": None,
            "error": "probe exploded",
        }
    ]
