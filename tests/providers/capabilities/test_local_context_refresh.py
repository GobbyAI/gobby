"""Refresh coordination for endpoint-scoped local context observations."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import cast

import httpx
import pytest

from gobby.providers.capabilities.local_context import (
    ContextDiagnostic,
    LocalContextIdentity,
    LocalContextInstance,
    LocalContextObservation,
    build_context_observation,
)
from gobby.providers.capabilities.local_context_config import LocalContextRoute
from gobby.providers.capabilities.local_context_refresh import LocalContextService
from gobby.providers.capabilities.local_context_store import LocalContextStore
from gobby.providers.capabilities.models import ProviderSnapshot
from gobby.providers.capabilities.store import ProviderCapabilityStore

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _MemoryStore:
    def __init__(self) -> None:
        self.snapshots: dict[tuple[str, str, str], tuple[LocalContextObservation, ...]] = {}
        self.thread_ids: list[int] = []
        self.block_next_write = False
        self.write_entered = threading.Event()
        self.release_write = threading.Event()
        self.release_write.set()
        self._lock = threading.Lock()

    def replace_endpoint_snapshot(
        self,
        *,
        machine_id: str,
        endpoint_id: str,
        configuration_fingerprint: str,
        base_url: str,
        observations: tuple[LocalContextObservation, ...],
    ) -> None:
        del base_url
        self.thread_ids.append(threading.get_ident())
        if self.block_next_write:
            self.block_next_write = False
            self.release_write.clear()
            self.write_entered.set()
            if not self.release_write.wait(timeout=1):
                raise TimeoutError("test did not release blocked persistence")
        with self._lock:
            self.snapshots[(machine_id, endpoint_id, configuration_fingerprint)] = observations

    def get_endpoint_observations(
        self,
        *,
        machine_id: str,
        endpoint_id: str,
        configuration_fingerprint: str,
    ) -> tuple[LocalContextObservation, ...]:
        self.thread_ids.append(threading.get_ident())
        with self._lock:
            return self.snapshots.get((machine_id, endpoint_id, configuration_fingerprint), ())

    def observation(self, route: LocalContextRoute) -> LocalContextObservation | None:
        observations = self.get_endpoint_observations(
            machine_id=route.machine_id,
            endpoint_id=route.endpoint_id,
            configuration_fingerprint=route.configuration_fingerprint,
        )
        return next(
            (observation for observation in observations if route.matches_observation(observation)),
            None,
        )

    def seed(self, route: LocalContextRoute, observation: LocalContextObservation) -> None:
        self.snapshots[(route.machine_id, route.endpoint_id, route.configuration_fingerprint)] = (
            observation,
        )


class _SnapshotStore:
    def __init__(self) -> None:
        self.snapshots: dict[str, ProviderSnapshot] = {}

    def replace_provider_snapshot(self, snapshot: ProviderSnapshot) -> None:
        self.snapshots[snapshot.provider] = snapshot

    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None:
        return self.snapshots.get(provider)


def _route() -> LocalContextRoute:
    return LocalContextRoute(
        machine_id="machine-a",
        endpoint_id="endpoint:local",
        configuration_fingerprint="config-a",
        provider="codex",
        protocol="openai-compatible",
        model_id="model-a",
        api_base="http://localhost:8000/v1",
        api_key="private-token",
        is_local=True,
    )


def _known_observation(
    identity: LocalContextIdentity,
    model_id: str,
    instance_id: str | None,
) -> LocalContextObservation:
    return build_context_observation(
        machine_id=identity.machine_id,
        endpoint_id=identity.endpoint_id,
        configuration_fingerprint=identity.configuration_fingerprint,
        provider="openai-compatible",
        model_id=model_id,
        instance_id=instance_id,
        instances=(
            LocalContextInstance(
                model_id=model_id,
                instance_id=instance_id,
                canonical_limit=8192,
                runtime_limit=4096,
            ),
        ),
    )


async def test_refresh_failure_recovery_coalescing() -> None:
    store = _MemoryStore()
    store.block_next_write = True
    calls = 0

    async def collect(
        _client: httpx.AsyncClient,
        identity: LocalContextIdentity,
        model_id: str,
        instance_id: str | None,
    ) -> LocalContextObservation:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("private endpoint failure")
        return _known_observation(identity, model_id, instance_id)

    route = _route()
    loop_thread = threading.get_ident()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as client:
        service = LocalContextService(
            store,
            client=client,
            collectors={"openai-compatible": collect},
        )
        cancelled_waiter = asyncio.create_task(service.refresh(route))
        await asyncio.wait_for(asyncio.to_thread(store.write_entered.wait), timeout=0.5)
        shared_waiter = asyncio.create_task(service.refresh(route))

        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter

        event_loop_tick = asyncio.Event()
        asyncio.get_running_loop().call_soon(event_loop_tick.set)
        await asyncio.wait_for(event_loop_tick.wait(), timeout=0.1)
        store.release_write.set()

        failed = await shared_waiter
        recovered = await service.refresh(route)

    assert calls == 2
    assert ContextDiagnostic.ENDPOINT_UNAVAILABLE in failed.diagnostics
    assert failed.effective_limit is None
    assert recovered.effective_limit == 4096
    assert await service.get_observation(route) == recovered
    assert store.thread_ids
    assert all(thread_id != loop_thread for thread_id in store.thread_ids)


@pytest.mark.parametrize(
    "change",
    ("configuration", "model", "instance"),
    ids=("configuration", "model", "instance"),
)
async def test_superseded_refresh(change: str) -> None:
    store = _MemoryStore()
    route_a = _route()
    if change == "configuration":
        route_b = replace(route_a, configuration_fingerprint="config-b")
    elif change == "model":
        route_b = replace(route_a, model_id="model-b")
    else:
        route_b = replace(route_a, instance_id="instance-b")
    stale_started = asyncio.Event()
    release_stale = asyncio.Event()

    async def collect(
        _client: httpx.AsyncClient,
        identity: LocalContextIdentity,
        model_id: str,
        instance_id: str | None,
    ) -> LocalContextObservation:
        if (
            identity.configuration_fingerprint == route_a.configuration_fingerprint
            and model_id == route_a.model_id
            and instance_id == route_a.instance_id
        ):
            stale_started.set()
            await release_stale.wait()
        return _known_observation(identity, model_id, instance_id)

    prior = _known_observation(route_a.identity, route_a.model_id, route_a.instance_id)
    store.seed(route_a, prior)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as client:
        service = LocalContextService(
            store,
            client=client,
            collectors={"openai-compatible": collect},
        )
        assert await service.get_observation(route_a) is None

        stale_task = asyncio.create_task(service.refresh(route_a))
        await asyncio.wait_for(stale_started.wait(), timeout=0.5)
        assert store.observation(route_a) is None

        current = await service.refresh(route_b)
        release_stale.set()
        stale = await stale_task

    assert current.effective_limit == 4096
    assert ContextDiagnostic.SUPERSEDED in stale.diagnostics
    assert stale.effective_limit is None
    assert await service.get_observation(route_a) is None
    assert await service.get_observation(route_b) == current


async def test_reselected_route_starts_a_new_generation() -> None:
    store = _MemoryStore()
    route_a = _route()
    route_b = replace(route_a, model_id="model-b")
    first_a_started = asyncio.Event()
    b_started = asyncio.Event()
    release_first_a = asyncio.Event()
    release_b = asyncio.Event()
    calls: list[str] = []

    async def collect(
        _client: httpx.AsyncClient,
        identity: LocalContextIdentity,
        model_id: str,
        instance_id: str | None,
    ) -> LocalContextObservation:
        calls.append(model_id)
        if model_id == route_a.model_id and calls.count(route_a.model_id) == 1:
            first_a_started.set()
            await release_first_a.wait()
        elif model_id == route_b.model_id:
            b_started.set()
            await release_b.wait()
        return _known_observation(identity, model_id, instance_id)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as client:
        service = LocalContextService(
            store,
            client=client,
            collectors={"openai-compatible": collect},
        )
        first_a = asyncio.create_task(service.refresh(route_a))
        await asyncio.wait_for(first_a_started.wait(), timeout=0.5)
        stale_b = asyncio.create_task(service.refresh(route_b))
        await asyncio.wait_for(b_started.wait(), timeout=0.5)

        current_a = await service.refresh(route_a)
        release_b.set()
        release_first_a.set()
        superseded_b, superseded_a = await asyncio.gather(stale_b, first_a)

    assert calls == ["model-a", "model-b", "model-a"]
    assert current_a.effective_limit == 4096
    assert ContextDiagnostic.SUPERSEDED in superseded_b.diagnostics
    assert ContextDiagnostic.SUPERSEDED in superseded_a.diagnostics
    assert await service.get_observation(route_a) == current_a


async def test_native_ollama_digest_refresh_is_retrievable() -> None:
    digest = "sha256:artifact-a"
    route = replace(
        _route(),
        protocol="ollama",
        api_base="http://localhost:11434/v1",
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer private-token"
        if request.url.path == "/api/show":
            return httpx.Response(
                200,
                json={
                    "digest": digest,
                    "model_info": {
                        "general.architecture": "qwen2",
                        "qwen2.context_length": 8192,
                    },
                },
            )
        assert request.url.path == "/api/ps"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "model": route.model_id,
                        "name": route.model_id,
                        "digest": digest,
                        "context_length": 4096,
                    }
                ]
            },
        )

    snapshot_store = cast(ProviderCapabilityStore, _SnapshotStore())
    store = LocalContextStore(snapshot_store)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = LocalContextService(store, client=client)
        refreshed = await service.refresh(route)

    assert refreshed.digest == digest
    assert refreshed.effective_limit == 4096
    assert await service.get_observation(route) == refreshed
