"""Codex app-server capability collector tests."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

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


def _collector(models: Sequence[Mapping[str, object]]) -> CodexCollector:
    async def fetch_models() -> Sequence[Mapping[str, object]]:
        return models

    return CodexCollector(fetch_models=fetch_models, clock=lambda: _OBSERVED_AT)


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
    assert model.context_length == 1_000_000


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
