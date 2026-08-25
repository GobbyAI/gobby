from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import pytest

from gobby.config.ai import GenerationEndpointConfig
from gobby.servers.generation_endpoint_health import GenerationEndpointHealthCoordinator
from gobby.servers.local_provider_models import generation_endpoint_probe_result

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
