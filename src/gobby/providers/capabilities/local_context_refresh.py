"""Refresh and publish exact local endpoint context observations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Protocol, cast

import httpx

from gobby.providers.capabilities.local_context import (
    ContextDiagnostic,
    LocalContextIdentity,
    LocalContextObservation,
    build_context_observation,
)
from gobby.providers.capabilities.local_context_config import LocalContextRoute
from gobby.providers.capabilities.local_context_generic import discover_generic_context
from gobby.providers.capabilities.local_context_lmstudio import discover_lmstudio_context
from gobby.providers.capabilities.local_context_ollama import discover_ollama_context
from gobby.providers.capabilities.local_context_vllm import discover_vllm_context

type RefreshKey = tuple[str, str, str, str, str, str | None, bool]
type EndpointKey = tuple[str, str]
type Collector = Callable[
    [httpx.AsyncClient, LocalContextIdentity, str, str | None],
    Awaitable[LocalContextObservation],
]
type RunDatabase = Callable[..., Awaitable[object]]


class ObservationStore(Protocol):
    """Synchronous persistence used by the local refresh service."""

    def replace_endpoint_snapshot(
        self,
        *,
        machine_id: str,
        endpoint_id: str,
        configuration_fingerprint: str,
        base_url: str,
        observations: tuple[LocalContextObservation, ...],
    ) -> None: ...

    def get_endpoint_observations(
        self,
        *,
        machine_id: str,
        endpoint_id: str,
        configuration_fingerprint: str,
    ) -> tuple[LocalContextObservation, ...]: ...


@dataclass
class _EndpointState:
    generation: int = 0
    current_key: RefreshKey | None = None
    current_route: LocalContextRoute | None = None
    current_task: asyncio.Task[LocalContextObservation] | None = None


class LocalContextService:
    """Own endpoint refresh coalescing, invalidation, and publication."""

    def __init__(
        self,
        store: ObservationStore,
        *,
        client: httpx.AsyncClient | None = None,
        collectors: Mapping[str, Collector] | None = None,
        run_db: RunDatabase | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._collectors = dict(_default_collectors() if collectors is None else collectors)
        self._run_db = run_db
        self._lock = asyncio.Lock()
        self._endpoint_states: dict[EndpointKey, _EndpointState] = {}
        self._fresh: set[RefreshKey] = set()

    async def refresh(self, route: LocalContextRoute) -> LocalContextObservation:
        """Refresh one route, sharing equivalent work without sharing cancellation."""
        key = route.refresh_key
        async with self._lock:
            endpoint_key = (route.machine_id, route.endpoint_id)
            state = self._endpoint_states.setdefault(endpoint_key, _EndpointState())
            if (
                state.current_key == key
                and state.current_task is not None
                and not state.current_task.done()
            ):
                task = state.current_task
            else:
                previous_route = state.current_route
                if state.current_key != key:
                    state.generation += 1
                    state.current_key = key
                    state.current_route = route
                task = asyncio.create_task(
                    self._refresh_and_publish(route, previous_route, state.generation)
                )
                state.current_task = task
        return await asyncio.shield(task)

    async def get_observation(self, route: LocalContextRoute) -> LocalContextObservation | None:
        """Read evidence refreshed by this service process for the current route."""
        key = route.refresh_key
        endpoint_key = (route.machine_id, route.endpoint_id)
        async with self._lock:
            state = self._endpoint_states.get(endpoint_key)
            if state is None or state.current_key != key or key not in self._fresh:
                return None
            observations = await self._run_store(
                partial(
                    self._store.get_endpoint_observations,
                    machine_id=route.machine_id,
                    endpoint_id=route.endpoint_id,
                    configuration_fingerprint=route.configuration_fingerprint,
                )
            )
            matching = tuple(
                observation
                for observation in observations
                if route.matches_observation(observation)
            )
            return matching[0] if len(matching) == 1 else None

    async def _refresh_and_publish(
        self,
        route: LocalContextRoute,
        previous_route: LocalContextRoute | None,
        token: int,
    ) -> LocalContextObservation:
        key = route.refresh_key
        endpoint_key = (route.machine_id, route.endpoint_id)
        try:
            if not await self._invalidate_current(route, previous_route, token):
                return _unknown_observation(route, ContextDiagnostic.SUPERSEDED)
            observation = await self._collect(route)
            if not route.matches_observation(observation):
                observation = _unknown_observation(route, ContextDiagnostic.INVALID_METADATA)
            async with self._lock:
                state = self._endpoint_states[endpoint_key]
                if state.generation != token or state.current_key != key:
                    return _unknown_observation(route, ContextDiagnostic.SUPERSEDED)
                await self._replace(route, (observation,))
                self._fresh.add(key)
                return observation
        finally:
            current = asyncio.current_task()
            async with self._lock:
                final_state = self._endpoint_states.get(endpoint_key)
                if final_state is not None and final_state.current_task is current:
                    final_state.current_task = None

    async def _invalidate_current(
        self,
        route: LocalContextRoute,
        previous_route: LocalContextRoute | None,
        token: int,
    ) -> bool:
        key = route.refresh_key
        endpoint_key = (route.machine_id, route.endpoint_id)
        async with self._lock:
            state = self._endpoint_states[endpoint_key]
            if state.generation != token or state.current_key != key:
                return False
            self._fresh.discard(key)
            to_clear = [route]
            if previous_route is not None and (
                previous_route.configuration_fingerprint != route.configuration_fingerprint
                or previous_route.endpoint_id != route.endpoint_id
            ):
                to_clear.append(previous_route)
            for stale_route in to_clear:
                await self._replace(stale_route, ())
            return True

    async def _collect(self, route: LocalContextRoute) -> LocalContextObservation:
        if not route.is_local:
            return _unknown_observation(route, ContextDiagnostic.NOT_LOCAL)
        collector = self._collectors.get(route.protocol)
        if collector is None:
            return _unknown_observation(route, ContextDiagnostic.INVALID_METADATA)
        try:
            if self._client is not None:
                return await collector(
                    self._client,
                    route.identity,
                    route.model_id,
                    route.instance_id,
                )
            async with httpx.AsyncClient() as client:
                return await collector(client, route.identity, route.model_id, route.instance_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _unknown_observation(route, ContextDiagnostic.ENDPOINT_UNAVAILABLE)

    async def _replace(
        self,
        route: LocalContextRoute,
        observations: tuple[LocalContextObservation, ...],
    ) -> None:
        await self._run_store(
            partial(
                self._store.replace_endpoint_snapshot,
                machine_id=route.machine_id,
                endpoint_id=route.endpoint_id,
                configuration_fingerprint=route.configuration_fingerprint,
                base_url=route.api_base,
                observations=observations,
            )
        )

    async def _run_store[T](self, operation: Callable[[], T]) -> T:
        if self._run_db is None:
            return await asyncio.to_thread(operation)
        return cast(T, await self._run_db(operation))


def _default_collectors() -> Mapping[str, Collector]:
    async def generic(
        client: httpx.AsyncClient,
        identity: LocalContextIdentity,
        model_id: str,
        _instance_id: str | None,
    ) -> LocalContextObservation:
        return await discover_generic_context(client, identity, model_id)

    async def lmstudio(
        client: httpx.AsyncClient,
        identity: LocalContextIdentity,
        model_id: str,
        instance_id: str | None,
    ) -> LocalContextObservation:
        return await discover_lmstudio_context(client, identity, model_id, instance_id)

    async def ollama(
        client: httpx.AsyncClient,
        identity: LocalContextIdentity,
        model_id: str,
        _instance_id: str | None,
    ) -> LocalContextObservation:
        return await discover_ollama_context(client, identity, model_id)

    async def vllm(
        client: httpx.AsyncClient,
        identity: LocalContextIdentity,
        model_id: str,
        _instance_id: str | None,
    ) -> LocalContextObservation:
        return await discover_vllm_context(client, identity, model_id)

    return {
        "openai-compatible": generic,
        "lmstudio": lmstudio,
        "ollama": ollama,
        "vllm": vllm,
    }


def _unknown_observation(
    route: LocalContextRoute,
    diagnostic: ContextDiagnostic,
) -> LocalContextObservation:
    return build_context_observation(
        machine_id=route.machine_id,
        endpoint_id=route.endpoint_id,
        configuration_fingerprint=route.configuration_fingerprint,
        provider=route.protocol,
        model_id=route.model_id,
        instance_id=route.instance_id,
        diagnostics=(diagnostic,),
    )
