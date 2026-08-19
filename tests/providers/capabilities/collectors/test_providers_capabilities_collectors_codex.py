"""Codex app-server capability collector tests."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.providers.capabilities.collectors.base import validate_snapshot
from gobby.providers.capabilities.collectors.codex import CodexCollector, CodexSourceError
from gobby.providers.capabilities.models import SourceState, SpeedMode
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.storage.hub.protocol import HubDatabase

_OBSERVED_AT = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _model(**overrides: object) -> dict[str, object]:
    model: dict[str, object] = {
        "id": "gpt-test",
        "model": "gpt-test",
        "displayName": "GPT Test",
        "hidden": False,
        "isDefault": True,
        "contextWindow": 128_000,
        "maxContextWindow": 1_000_000,
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"},
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "high"},
        ],
        "defaultReasoningEffort": "medium",
        "inputModalities": ["text", "image"],
        "serviceTiers": [],
        "additionalSpeedTiers": [],
    }
    model.update(overrides)
    return model


def _collector(
    models: Sequence[Mapping[str, object]],
    cache_context_lengths: Mapping[str, int] | None = None,
) -> CodexCollector:
    async def fetch_models() -> Sequence[Mapping[str, object]]:
        return models

    async def fetch_models_cache() -> Mapping[str, int]:
        return cache_context_lengths or {}

    return CodexCollector(
        fetch_models=fetch_models,
        fetch_models_cache=fetch_models_cache,
        clock=lambda: _OBSERVED_AT,
    )


@pytest.mark.asyncio
async def test_fast_tier_same_selector_route() -> None:
    collector = _collector(
        [
            _model(
                serviceTiers=[
                    {
                        "id": "priority",
                        "name": "Fast",
                        "description": "1.5x speed, increased usage",
                    }
                ]
            )
        ]
    )

    snapshot = await collector.collect()

    validate_snapshot(snapshot, collector.sources)
    model = snapshot.models[0]
    standard = next(route for route in model.routes if route.speed_mode is SpeedMode.STANDARD)
    fast = next(route for route in model.routes if route.speed_mode is SpeedMode.FAST)
    assert standard.selector == fast.selector == "gpt-test"
    assert [activation.to_dict() for activation in fast.activations] == [
        {
            "kind": "request_parameter",
            "surface": "app-server",
            "params": {"name": "serviceTier", "value": "priority"},
        }
    ]
    assert model.context_length == 128_000


@pytest.mark.asyncio
async def test_models_cache_enriches_missing_context_lengths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_models = [
        {
            "slug": "gpt-5.3-codex-spark",
            "context_window": 128_000,
            "max_context_window": 1_000_000,
        },
        {
            "slug": "codex-auto-review",
            "context_window": 272_000,
            "max_context_window": 1_000_000,
        },
        {
            "slug": "gpt-5.6-sol-wm",
            "context_window": 272_000,
            "max_context_window": 1_000_000,
        },
    ]
    (tmp_path / "models_cache.json").write_text(
        json.dumps({"models": cache_models}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    raw_models = [
        _model(
            id=entry["slug"],
            model=entry["slug"],
            displayName=entry["slug"],
            contextWindow=None,
            maxContextWindow=None,
        )
        for entry in cache_models
    ]

    async def fetch_models() -> Sequence[Mapping[str, object]]:
        return raw_models

    collector = CodexCollector(fetch_models=fetch_models, clock=lambda: _OBSERVED_AT)
    snapshot = await collector.collect()

    validate_snapshot(snapshot, collector.sources)
    context_lengths = {model.canonical_model: model.context_length for model in snapshot.models}
    assert context_lengths == {
        "gpt-5.3-codex-spark": 128_000,
        "codex-auto-review": 272_000,
        "gpt-5.6-sol-wm": 272_000,
    }
    for model in snapshot.models:
        assert model.provenance["context_length"].source_key == "models-cache"
        assert model.provenance["display_name"].source_key == "app-server-model-list"
    assert snapshot.sources[1].state is SourceState.OK
    assert snapshot.sources[1].required is False


@pytest.mark.asyncio
async def test_app_server_context_precedes_conflicting_cache_value() -> None:
    collector = _collector([_model()], {"gpt-test": 64_000})

    snapshot = await collector.collect()

    model = snapshot.models[0]
    assert model.context_length == 128_000
    assert model.provenance["context_length"].source_key == "app-server-model-list"


@pytest.mark.asyncio
async def test_app_server_max_context_precedes_conflicting_cache_value() -> None:
    collector = _collector(
        [_model(contextWindow=None, maxContextWindow=256_000)],
        {"gpt-test": 64_000},
    )

    snapshot = await collector.collect()

    assert snapshot.models[0].context_length == 256_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cache_contents",
    [pytest.param(None, id="missing"), pytest.param("{malformed", id="malformed")],
)
async def test_unavailable_models_cache_is_optional(
    cache_contents: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if cache_contents is not None:
        (tmp_path / "models_cache.json").write_text(cache_contents, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    async def fetch_models() -> Sequence[Mapping[str, object]]:
        return [_model()]

    collector = CodexCollector(fetch_models=fetch_models, clock=lambda: _OBSERVED_AT)
    snapshot = await collector.collect()

    validate_snapshot(snapshot, collector.sources)
    assert snapshot.models[0].context_length == 128_000
    assert snapshot.sources[0].state is SourceState.OK
    cache_health = snapshot.sources[1]
    assert cache_health.state is SourceState.ERROR
    assert cache_health.required is False
    assert cache_health.last_success_at is None
    assert cache_health.last_error is not None


@pytest.mark.asyncio
async def test_model_absent_from_cache_retains_unknown_context() -> None:
    collector = _collector(
        [_model(contextWindow=None, maxContextWindow=None)],
        {"different-model": 64_000},
    )

    snapshot = await collector.collect()

    model = snapshot.models[0]
    assert model.context_length is None
    assert "context_length" not in model.provenance


@pytest.mark.asyncio
async def test_reasoning_effort_variants() -> None:
    efforts = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    collector = _collector(
        [
            _model(
                supportedReasoningEfforts=[{"reasoningEffort": effort} for effort in efforts],
                defaultReasoningEffort="high",
            )
        ]
    )

    snapshot = await collector.collect()

    validate_snapshot(snapshot, collector.sources)
    assert snapshot.models[0].supported_efforts == efforts
    assert snapshot.models[0].default_effort == "high"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_app_server_down_retains_last_good(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)
    store.replace_provider_snapshot(await _collector([_model()]).collect())
    before = store.get_provider_snapshot("codex")
    assert before is not None

    async def app_server_down() -> Sequence[Mapping[str, object]]:
        raise FileNotFoundError("codex executable is unavailable")

    with pytest.raises(CodexSourceError, match="codex executable is unavailable"):
        await CodexCollector(
            fetch_models=app_server_down,
            clock=lambda: _OBSERVED_AT,
        ).collect()

    after = store.get_provider_snapshot("codex")
    assert after is not None
    assert after.models == before.models
    assert after.generation == before.generation
    assert after.sources[0].state is SourceState.OK
