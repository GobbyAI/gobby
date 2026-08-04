from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from gobby.providers.capabilities.collectors import SourceSpec
from gobby.providers.capabilities.models import (
    FactProvenance,
    ModelCapability,
    ModelRoute,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
    SpeedMode,
)
from gobby.providers.capabilities.refresh import CapabilityRefreshCoordinator
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.storage.hub.protocol import HubDatabase


def _snapshot(provider: str, *model_names: str) -> ProviderSnapshot:
    observed_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    source_key = "provider-api"
    source_url = f"https://example.test/{provider}/models"
    fact = FactProvenance(
        source_key=source_key,
        source_url=source_url,
        observed_at=observed_at,
    )
    provenance = dict.fromkeys(
        (
            "canonical_model",
            "display_name",
            "aliases",
            "available",
            "hidden",
            "is_default",
            "context_length",
            "max_output_tokens",
            "reasoning",
            "supported_efforts",
            "default_effort",
            "latency_class",
            "input_modalities",
            "supports_tools",
            "speed_mode",
            "selector",
            "activations",
            "usage_multiplier",
        ),
        fact,
    )
    models = tuple(
        ModelCapability(
            canonical_model=model_name,
            display_name=model_name,
            aliases=(),
            available=True,
            hidden=False,
            is_default=index == 0,
            context_length=128_000,
            max_output_tokens=16_000,
            reasoning=ReasoningSupport.KNOWN,
            supported_efforts=("low", "medium", "high"),
            default_effort="medium",
            latency_class="normal",
            input_modalities=("text",),
            supports_tools=True,
            routes=(
                ModelRoute(
                    speed_mode=SpeedMode.STANDARD,
                    selector=model_name,
                    available=True,
                    usage_multiplier=Decimal("1"),
                    throughput_multiplier=None,
                    latency_class="normal",
                    activations=(),
                    provenance=provenance,
                ),
            ),
            provenance=provenance,
        )
        for index, model_name in enumerate(model_names)
    )
    return ProviderSnapshot(
        provider=provider,
        generation=0,
        models=models,
        sources=(
            SourceHealth(
                source_key=source_key,
                source_url=source_url,
                required=True,
                state=SourceState.OK,
                attempts=1,
                last_attempt_at=observed_at,
                last_success_at=observed_at,
                last_error=None,
            ),
        ),
    )


class _MemoryStore:
    def __init__(self, snapshot: ProviderSnapshot) -> None:
        self.snapshot = snapshot
        self.failures: list[tuple[str, str, str]] = []

    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None:
        return self.snapshot if self.snapshot.provider == provider else None

    def get_all_snapshots(self) -> tuple[ProviderSnapshot, ...]:
        return (self.snapshot,)

    def replace_provider_snapshot(self, snapshot: ProviderSnapshot) -> None:
        self.snapshot = snapshot

    def record_source_failure(self, provider: str, source_key: str, error: str) -> None:
        self.failures.append((provider, source_key, error))
        source = next(item for item in self.snapshot.sources if item.source_key == source_key)
        self.snapshot = replace(
            self.snapshot,
            sources=(
                replace(
                    source,
                    state=SourceState.STALE,
                    attempts=source.attempts + 1,
                    last_error=error,
                ),
            ),
        )


class _Collector:
    provider: str
    sources: tuple[SourceSpec, ...]

    def __init__(
        self,
        collect: Callable[[], Awaitable[ProviderSnapshot]],
        provider: str = "codex",
    ) -> None:
        self._collect = collect
        self.provider = provider
        self.sources = (
            SourceSpec(
                "provider-api",
                f"https://example.test/{provider}/models",
                required=True,
            ),
        )

    async def collect(self) -> ProviderSnapshot:
        return await self._collect()


@pytest.mark.asyncio
async def test_startup_nonblocking() -> None:
    prior = _snapshot("codex", "gpt-old")
    store = _MemoryStore(prior)
    started = asyncio.Event()
    release = asyncio.Event()

    async def collect() -> ProviderSnapshot:
        started.set()
        await release.wait()
        return _snapshot("codex", "gpt-new")

    coordinator = CapabilityRefreshCoordinator(store, {"codex": _Collector(collect)})
    refresh = asyncio.create_task(coordinator.refresh_all())
    await started.wait()

    assert coordinator.get_provider_snapshot("codex") == prior
    assert not refresh.done()

    release.set()
    await refresh


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ("network unavailable", "response was empty", "malformed response"),
)
async def test_failed_refresh_retains_last_good(message: str) -> None:
    prior = _snapshot("codex", "gpt-old")
    store = _MemoryStore(prior)

    async def collect() -> ProviderSnapshot:
        raise ValueError(message)

    coordinator = CapabilityRefreshCoordinator(store, {"codex": _Collector(collect)})

    await coordinator.refresh_all()

    current = coordinator.get_provider_snapshot("codex")
    assert current is not None
    assert current.models == prior.models
    assert current.generation == prior.generation
    assert current.sources[0].state is SourceState.STALE
    assert current.sources[0].attempts == 2
    assert store.failures == [("codex", "provider-api", message)]


@pytest.mark.asyncio
async def test_source_timeout_records_attempt() -> None:
    store = _MemoryStore(_snapshot("codex", "gpt-old"))

    async def collect() -> ProviderSnapshot:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    coordinator = CapabilityRefreshCoordinator(
        store,
        {"codex": _Collector(collect)},
        source_timeout_seconds=0.01,
    )

    await coordinator.refresh_all()

    assert store.failures == [("codex", "provider-api", "timed out after 0.01 seconds")]
    assert store.snapshot.sources[0].attempts == 2


@pytest.mark.asyncio
async def test_refreshes_providers_concurrently() -> None:
    store = _MemoryStore(_snapshot("codex", "gpt-old"))
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def collect(provider: str) -> ProviderSnapshot:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await release.wait()
        return _snapshot(provider, f"{provider}-new")

    coordinator = CapabilityRefreshCoordinator(
        store,
        {
            "codex": _Collector(lambda: collect("codex")),
            "qwen": _Collector(lambda: collect("qwen"), provider="qwen"),
        },
    )
    refresh = asyncio.create_task(coordinator.refresh_all())

    await asyncio.wait_for(both_started.wait(), timeout=0.2)
    release.set()
    await refresh

    assert started == 2


@pytest.mark.asyncio
async def test_schedule_immediate_then_daily() -> None:
    store = _MemoryStore(_snapshot("codex", "gpt-old"))
    attempts = 0
    delays: list[float] = []
    stopped = False

    async def collect() -> ProviderSnapshot:
        nonlocal attempts
        attempts += 1
        return _snapshot("codex", f"gpt-{attempts}")

    async def sleep(delay: float) -> None:
        nonlocal stopped
        delays.append(delay)
        if len(delays) == 2:
            stopped = True

    coordinator = CapabilityRefreshCoordinator(
        store,
        {"codex": _Collector(collect)},
        sleep=sleep,
    )

    await coordinator.run(lambda: stopped)

    assert attempts == 2
    assert delays == [86_400.0, 86_400.0]


@pytest.mark.asyncio
async def test_atomic_snapshot_swap(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)
    store.replace_provider_snapshot(_snapshot("codex", "gpt-old"))
    release = asyncio.Event()

    async def collect() -> ProviderSnapshot:
        await release.wait()
        return _snapshot("codex", "gpt-new-a", "gpt-new-b")

    coordinator = CapabilityRefreshCoordinator(store, {"codex": _Collector(collect)})
    refresh = asyncio.create_task(coordinator.refresh_all())
    observed: list[tuple[str, ...]] = []

    old = await asyncio.to_thread(store.get_provider_snapshot, "codex")
    assert old is not None
    observed.append(tuple(model.canonical_model for model in old.models))
    release.set()
    while not refresh.done():
        current = await asyncio.to_thread(store.get_provider_snapshot, "codex")
        assert current is not None
        observed.append(tuple(model.canonical_model for model in current.models))
    await refresh
    current = await asyncio.to_thread(store.get_provider_snapshot, "codex")
    assert current is not None
    observed.append(tuple(model.canonical_model for model in current.models))

    assert set(observed) <= {("gpt-old",), ("gpt-new-a", "gpt-new-b")}
    assert observed[0] == ("gpt-old",)
    assert observed[-1] == ("gpt-new-a", "gpt-new-b")
