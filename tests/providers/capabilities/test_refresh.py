from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from gobby.config.ai import ModelMetadataAlias
from gobby.providers.capabilities.collectors import CapabilityCollector, SourceSpec
from gobby.providers.capabilities.collectors.claude import (
    EFFORT_DOCS_URL,
    MODEL_CONFIG_URL,
    MODELS_OVERVIEW_URL,
    ClaudeCollector,
)
from gobby.providers.capabilities.coverage import ModelMetadataCoverageAuditor
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
from gobby.servers.provider_model_discovery import (
    claude_uses_loopback_model_endpoint,
    codex_uses_loopback_model_endpoint,
    qwen_local_model_values,
)
from gobby.storage.hub.protocol import HubDatabase


def _snapshot(
    provider: str,
    *model_names: str,
    context_length: int | None = 128_000,
) -> ProviderSnapshot:
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
            context_length=context_length,
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

    def has_rows(self, provider: str) -> bool:
        return True

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


class _MetadataStore:
    def __init__(self, contexts: dict[str, int] | None = None) -> None:
        self.contexts = dict(contexts or {})

    def get_context_window(self, model: str) -> int | None:
        return self.contexts.get(model)


class _CoverageAuditorSpy:
    def __init__(self) -> None:
        self.sync_calls = 0
        self.async_calls = 0

    def audit(self) -> None:
        self.sync_calls += 1

    async def audit_async(self) -> None:
        self.async_calls += 1


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
async def test_startup_and_successful_refresh_run_coverage_audits() -> None:
    prior = _snapshot("synthetic", "old-model")
    store = _MemoryStore(prior)
    auditor = _CoverageAuditorSpy()
    coordinator = CapabilityRefreshCoordinator(
        store,
        {
            "synthetic": _Collector(
                lambda: _async_snapshot("synthetic", "new-model"),
                provider="synthetic",
            )
        },
        coverage_auditor=auditor,
    )

    coordinator.prepare()
    await coordinator.refresh_all()

    assert auditor.sync_calls == 1
    assert auditor.async_calls == 1


async def test_claude_refresh_with_compatibility_effort_docs_emits_no_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixtures = Path(__file__).parent / "collectors" / "fixtures" / "claude"
    source_keys = {
        MODELS_OVERVIEW_URL: "models-overview",
        MODEL_CONFIG_URL: "model-config",
        EFFORT_DOCS_URL: "effort-docs",
    }
    documents = {
        source_key: (fixtures / f"{source_key}.md").read_text()
        for source_key in source_keys.values()
    }

    async def fetch_text(url: str) -> str:
        return documents[source_keys[url]]

    collector = ClaudeCollector(
        fetch_text=fetch_text,
        clock=lambda: datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )
    store = _MemoryStore(_snapshot("claude", "claude-opus-5"))
    coordinator = CapabilityRefreshCoordinator(
        store,
        provider_collectors={"claude": cast(CapabilityCollector, collector)},
    )

    with caplog.at_level(logging.WARNING):
        await coordinator.refresh_all()

    models = {model.canonical_model: model for model in store.snapshot.models}
    assert models["claude-opus-5"].supported_efforts == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert store.failures == []
    assert caplog.records == []


async def _async_snapshot(provider: str, model: str) -> ProviderSnapshot:
    return _snapshot(provider, model)


def test_coverage_audit_bounds_deduplicates_and_logs_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    model_ids = tuple(f"model-{index:02d}" for index in range(12))
    store = _MemoryStore(_snapshot("synthetic", *model_ids, context_length=None))
    metadata = _MetadataStore()
    auditor = ModelMetadataCoverageAuditor(store, metadata, [])

    with caplog.at_level(logging.INFO, logger="gobby.providers.capabilities.coverage"):
        auditor.audit()
        first_record_count = len(caplog.records)
        auditor.audit()

        metadata.contexts.update(dict.fromkeys(model_ids, 64_000))
        auditor.audit()

    coverage_records = [
        record
        for record in caplog.records
        if "models without context metadata" in record.getMessage()
    ]
    assert len(coverage_records) == 1
    assert coverage_records[0].levelno == logging.INFO
    coverage_message = coverage_records[0].getMessage()
    assert "model-00" in coverage_message
    assert "model-09" in coverage_message
    assert "model-10" not in coverage_message
    assert "; 2 omitted" in coverage_message
    assert first_record_count == 1
    assert "Provider synthetic context metadata coverage recovered" in [
        record.getMessage() for record in caplog.records
    ]


def test_coverage_audit_warns_for_missing_alias_target_and_logs_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _MemoryStore(_snapshot("synthetic", "provider-model", context_length=None))
    metadata = _MetadataStore()
    aliases = [
        ModelMetadataAlias(
            provider="synthetic",
            provider_model_id="provider-model",
            openrouter_model_id="vendor/registry-model",
        )
    ]
    auditor = ModelMetadataCoverageAuditor(store, metadata, aliases)

    with caplog.at_level(logging.INFO, logger="gobby.providers.capabilities.coverage"):
        auditor.audit()
        auditor.audit()
        metadata.contexts["vendor/registry-model"] = 64_000
        auditor.audit()

    messages = [record.getMessage() for record in caplog.records]
    assert sum("models without context metadata" in message for message in messages) == 1
    assert sum("configured alias targets missing" in message for message in messages) == 1
    assert "Provider synthetic context metadata coverage recovered" in messages
    assert "Provider synthetic model metadata alias targets recovered" in messages


def test_coverage_audit_skips_configured_local_models(
    caplog: pytest.LogCaptureFixture,
) -> None:
    local_model = "local-model(openai)"
    remote_model = "remote-model(openai)"
    store = _MemoryStore(_snapshot("qwen", local_model, remote_model, context_length=None))
    auditor = ModelMetadataCoverageAuditor(
        store,
        _MetadataStore(),
        [],
        excluded_models=lambda: frozenset({("qwen", local_model)}),
    )

    with caplog.at_level(logging.INFO, logger="gobby.providers.capabilities.coverage"):
        auditor.audit()

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert remote_model in messages[0]
    assert local_model not in messages[0]


def test_coverage_audit_skips_provider_using_local_endpoint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _MemoryStore(_snapshot("synthetic", "local-model", context_length=None))
    auditor = ModelMetadataCoverageAuditor(
        store,
        _MetadataStore(),
        [],
        excluded_providers=lambda: frozenset({"synthetic"}),
    )

    with caplog.at_level(logging.WARNING, logger="gobby.providers.capabilities.coverage"):
        auditor.audit()

    assert not caplog.records


def test_qwen_local_model_values_uses_loopback_base_urls() -> None:
    settings = {
        "modelProviders": {
            "openai": [
                {
                    "id": "lm-studio-model",
                    "baseUrl": "http://127.0.0.1:1234/v1",
                },
                {
                    "id": "remote-model",
                    "baseUrl": "https://models.example.test/v1",
                },
            ],
            "anthropic": [
                {
                    "id": "ollama-model",
                    "baseUrl": "http://[::1]:11434/v1",
                }
            ],
        }
    }

    assert qwen_local_model_values(settings) == frozenset(
        {"lm-studio-model(openai)", "ollama-model(anthropic)"}
    )


def test_codex_detects_active_loopback_model_provider() -> None:
    config = {
        "model_provider": "local-endpoint",
        "model_providers": {
            "local-endpoint": {"base_url": "http://localhost:1234/v1"},
            "remote-endpoint": {"base_url": "https://models.example.test/v1"},
        },
    }

    assert codex_uses_loopback_model_endpoint(config) is True
    assert (
        codex_uses_loopback_model_endpoint({**config, "model_provider": "remote-endpoint"}) is False
    )


def test_claude_detects_effective_loopback_model_endpoint() -> None:
    settings = {"env": {"ANTHROPIC_BASE_URL": "http://[::1]:1234/v1"}}

    assert claude_uses_loopback_model_endpoint(settings, environment={}) is True
    assert (
        claude_uses_loopback_model_endpoint(
            settings,
            environment={"ANTHROPIC_BASE_URL": "https://models.example.test/v1"},
        )
        is False
    )


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


async def test_authentication_required_refresh_is_informational(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def collect() -> ProviderSnapshot:
        raise ValueError("ACP session/new error: Authentication required")

    coordinator = CapabilityRefreshCoordinator(
        _MemoryStore(_snapshot("qwen", "qwen-seed")),
        {"qwen": _Collector(collect, provider="qwen")},
    )

    with caplog.at_level(logging.INFO, logger="gobby.providers.capabilities.refresh"):
        await coordinator.refresh_all()

    refresh_records = [
        record
        for record in caplog.records
        if "Provider capability refresh failed" in record.message
    ]
    assert len(refresh_records) == 1
    assert refresh_records[0].levelno == logging.INFO


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
