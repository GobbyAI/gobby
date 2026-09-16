import json
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gobby.adapters.qwen_acp_client import QwenACPClient
from gobby.agents.trust import authorize_model_discovery_trust
from gobby.providers.capabilities.collectors import qwen as qwen_capabilities
from gobby.providers.capabilities.collectors import validate_snapshot
from gobby.providers.capabilities.collectors.grok import GrokCollector, GrokSourceError
from gobby.providers.capabilities.collectors.qwen import QwenCollector, QwenSourceError
from gobby.providers.capabilities.models import ReasoningSupport, SourceState
from gobby.servers.provider_models_grok import models_from_cache

_OBSERVED_AT = datetime(2026, 8, 4, 15, tzinfo=UTC)
_GROK_CACHE_SUMMARY = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "provider_contracts"
    / "grok"
    / "model-cache-summary.json"
)
type RawModel = Mapping[str, object]
type DiscoverModels = Callable[[], Awaitable[Sequence[RawModel]]]
type FetchModelsCache = Callable[[], Awaitable[Sequence[RawModel]]]


def _discoverer(*models: RawModel) -> DiscoverModels:
    async def discover() -> Sequence[RawModel]:
        return models

    return discover


def _cache_loader(*models: RawModel) -> FetchModelsCache:
    async def load_cache() -> Sequence[RawModel]:
        return models

    return load_cache


async def test_standard_only_discovery() -> None:
    grok = GrokCollector(
        fetch_models_cache=_cache_loader(),
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
        fetch_models_cache=_cache_loader(),
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


async def test_models_cache_fills_missing_windows_and_omitted_models(
    tmp_path: Path,
) -> None:
    summary = json.loads(_GROK_CACHE_SUMMARY.read_text(encoding="utf-8"))
    assert "models" in summary["models_cache_keys"]
    cache_path = tmp_path / "models_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "modelId": "grok-4.6",
                        "name": "Grok 4.6",
                        "_meta": {"totalContextTokens": 500_000},
                    },
                    {
                        "modelId": "grok-build",
                        "name": "Grok Build",
                        "_meta": {"totalContextTokens": 512_000},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    async def load_cache() -> Sequence[RawModel]:
        return models_from_cache(cache_path)

    grok = GrokCollector(
        discover_models=_discoverer({"value": "grok-build", "label": "Grok Build"}),
        fetch_models_cache=load_cache,
        clock=lambda: _OBSERVED_AT,
    )
    snapshot = validate_snapshot(await grok.collect(), grok.sources)
    models = {model.canonical_model: model for model in snapshot.models}
    assert models["grok-build"].context_length == 512_000
    assert models["grok-4.6"].context_length == 500_000
    assert models["grok-4.6"].provenance["context_length"].source_key == "models-cache"
    assert snapshot.sources[1].source_key == "models-cache"
    assert snapshot.sources[1].state is SourceState.OK
    assert snapshot.sources[1].required is False


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
async def test_qwen_discovery_uses_acp_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discover_acp = AsyncMock(
        return_value=[
            {"value": "acp-local(openai)", "label": "ACP Local (openai)"},
            {"value": "oauth-model(qwen-oauth)", "label": "OAuth (qwen-oauth)"},
        ]
    )
    monkeypatch.setattr(qwen_capabilities, "discover_acp_models", discover_acp)
    collector = QwenCollector(clock=lambda: _OBSERVED_AT)

    snapshot = validate_snapshot(await collector.collect(), collector.sources)

    assert {model.canonical_model for model in snapshot.models} == {
        "acp-local(openai)",
        "oauth-model(qwen-oauth)",
    }
    discover_acp.assert_awaited_once_with(
        client_cls=QwenACPClient,
        which=shutil.which,
        model_discovery_cwd=qwen_capabilities._model_discovery_cwd,
        authorize_trust=authorize_model_discovery_trust,
        cleanup_tree=shutil.rmtree,
        logger=qwen_capabilities.logger,
    )

    discover_acp.side_effect = RuntimeError("ACP discovery failed")
    with pytest.raises(QwenSourceError, match="ACP discovery failed"):
        await collector.collect()
