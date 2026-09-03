from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from gobby.providers.capabilities.collectors import qwen as qwen_capabilities
from gobby.providers.capabilities.collectors import validate_snapshot
from gobby.providers.capabilities.collectors.grok import GrokCollector, GrokSourceError
from gobby.providers.capabilities.collectors.qwen import QwenCollector, QwenSourceError
from gobby.providers.capabilities.models import ReasoningSupport

_OBSERVED_AT = datetime(2026, 8, 4, 15, tzinfo=UTC)
type RawModel = Mapping[str, object]
type DiscoverModels = Callable[[], Awaitable[Sequence[RawModel]]]


def _discoverer(*models: RawModel) -> DiscoverModels:
    async def discover() -> Sequence[RawModel]:
        return models

    return discover


async def test_standard_only_discovery() -> None:
    grok = GrokCollector(
        discover_models=_discoverer(
            {
                "value": "grok-composer-2.5-fast",
                "label": "Grok Composer 2.5 Fast",
                "context_length": 200_000,
            },
            {
                "value": "grok-build",
                "label": "Grok Build",
                "context_length": 512_000,
            },
        ),
        clock=lambda: _OBSERVED_AT,
    )
    qwen = QwenCollector(
        discover_models=_discoverer(
            {
                "value": "qwen3-coder-plus",
                "label": "Qwen3 Coder Plus",
                "context_length": 262_144,
                "reasoning": {
                    "supported_efforts": ["low", "medium", "high"],
                    "default_effort": "medium",
                },
            }
        ),
        clock=lambda: _OBSERVED_AT,
    )

    snapshots = (
        validate_snapshot(await grok.collect(), grok.sources),
        validate_snapshot(await qwen.collect(), qwen.sources),
    )

    grok_models = {model.canonical_model: model for model in snapshots[0].models}
    assert grok_models["grok-composer-2.5-fast"].context_length == 200_000
    assert grok_models["grok-build"].context_length == 512_000
    assert snapshots[1].models[0].context_length == 262_144
    assert snapshots[1].models[0].reasoning is ReasoningSupport.KNOWN
    assert snapshots[1].models[0].supported_efforts == ("low", "medium", "high")
    assert snapshots[1].models[0].default_effort == "medium"


async def test_unknown_reasoning_null_efforts() -> None:
    grok = GrokCollector(
        discover_models=_discoverer({"value": "grok-build", "label": "Grok Build"}),
        clock=lambda: _OBSERVED_AT,
    )
    qwen = QwenCollector(
        discover_models=_discoverer({"value": "coder-model", "label": "Qwen Coder"}),
        clock=lambda: _OBSERVED_AT,
    )

    models = (
        (await grok.collect()).models[0],
        (await qwen.collect()).models[0],
    )

    for model in models:
        assert model.reasoning is ReasoningSupport.UNKNOWN
        assert model.supported_efforts is None
        assert model.default_effort is None


async def test_missing_cli_is_source_failure() -> None:
    async def missing_cli() -> Sequence[RawModel]:
        raise FileNotFoundError("CLI not found in PATH")

    with pytest.raises(GrokSourceError, match="CLI not found in PATH") as grok_error:
        await GrokCollector(discover_models=missing_cli).collect()
    with pytest.raises(QwenSourceError, match="CLI not found in PATH") as qwen_error:
        await QwenCollector(discover_models=missing_cli).collect()

    assert grok_error.value.source_key == GrokCollector.sources[0].source_key
    assert qwen_error.value.source_key == QwenCollector.sources[0].source_key


@pytest.mark.asyncio
async def test_qwen_discovery_filters_loopback_models_after_sources_are_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = {
        "modelProviders": {
            "openai": [
                {
                    "id": "acp-local",
                    "name": "ACP Local",
                    "baseUrl": "http://localhost:1234/v1",
                },
                {
                    "id": "configured-local",
                    "baseUrl": "http://127.0.0.1:11434/v1",
                },
                {
                    "id": "remote-configured",
                    "baseUrl": "https://models.example.test/v1",
                },
            ],
            "anthropic": [
                {
                    "id": "ipv6-local",
                    "baseUrl": "http://[::1]:8080/v1",
                }
            ],
        }
    }
    settings_loader = MagicMock(return_value=settings)

    async def discover_acp(_client_cls: object) -> list[dict[str, object]]:
        return [
            {"value": "acp-local(openai)", "label": "ACP Local (openai)"},
            {"value": "oauth-model(qwen-oauth)", "label": "OAuth (qwen-oauth)"},
        ]

    monkeypatch.setattr(qwen_capabilities, "load_qwen_settings", settings_loader)
    monkeypatch.setattr(qwen_capabilities, "_discover_acp", discover_acp)
    monkeypatch.setattr(
        "gobby.providers.capabilities.collectors.qwen.shutil.which",
        lambda _name: "/usr/bin/qwen",
    )

    models = await qwen_capabilities._discover_qwen_models()

    assert len(models) == 2
    assert {model["value"] for model in models} == {
        "oauth-model(qwen-oauth)",
        "remote-configured(openai)",
    }
    settings_loader.assert_called_once()
