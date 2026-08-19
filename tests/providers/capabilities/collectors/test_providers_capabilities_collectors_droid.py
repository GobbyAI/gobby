from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from gobby.llm.context_windows import resolve_context_window_with_source
from gobby.providers.capabilities.collectors import validate_snapshot
from gobby.providers.capabilities.collectors.droid import (
    DROID_MODELS_URL,
    DroidCollector,
)
from gobby.providers.capabilities.models import ReasoningSupport, SpeedMode

_OBSERVED_AT = datetime(2026, 8, 4, 12, tzinfo=UTC)
_MODELS_DOCUMENT = """
| Model | Model ID | Multiplier | Reasoning |
| --- | --- | --- | --- |
| GPT-5.5 | `gpt-5.5` | 2× | `none`, `low`, `medium` (default), `high`, `xhigh` |
| GPT-5.5 Fast | `gpt-5.5-fast` | 5× | `none`, `low`, `medium` (default), `high`, `xhigh` |
| Turbo Row | `suffix-only-fast` | 3× | `minimal` (default), `high` |
| GLM-5.2 Fast Mode | `glm-5.2-fast` | 0.84× | `off`, `high` (default), `max` |
| Registry Only | `registry-only` | 0.25× | `off` (default), `high` |
"""


def _collector(document: str = _MODELS_DOCUMENT) -> DroidCollector:
    async def fetch_text(url: str) -> str:
        assert url == DROID_MODELS_URL
        return document

    return DroidCollector(fetch_text=fetch_text, clock=lambda: _OBSERVED_AT)


async def test_usage_multiplier_parsed() -> None:
    collector = _collector()
    snapshot = validate_snapshot(await collector.collect(), collector.sources)

    model = next(model for model in snapshot.models if model.canonical_model == "gpt-5.5")
    routes = {route.speed_mode: route for route in model.routes}

    assert routes[SpeedMode.STANDARD].usage_multiplier == Decimal("2")
    assert routes[SpeedMode.FAST].usage_multiplier == Decimal("5")
    assert model.supported_efforts == ("none", "low", "medium", "high", "xhigh")
    assert model.default_effort == "medium"
    assert model.reasoning is ReasoningSupport.KNOWN


async def test_fast_pairing_requires_explicit_label_and_standard_match() -> None:
    collector = _collector()
    snapshot = validate_snapshot(await collector.collect(), collector.sources)
    models = {model.canonical_model: model for model in snapshot.models}

    paired_routes = {route.speed_mode: route for route in models["gpt-5.5"].routes}
    assert paired_routes[SpeedMode.FAST].selector == "gpt-5.5-fast"
    assert {
        (activation.kind, activation.surface)
        for activation in paired_routes[SpeedMode.FAST].activations
    } == {("model_selector", "spawn-cli"), ("model_selector", "tool-chat")}
    assert "gpt-5.5-fast" not in models

    assert tuple(route.speed_mode for route in models["suffix-only-fast"].routes) == (
        SpeedMode.STANDARD,
    )
    assert models["suffix-only-fast"].supported_efforts == ("minimal", "high")
    assert models["suffix-only-fast"].default_effort == "minimal"
    assert tuple(route.speed_mode for route in models["glm-5.2-fast"].routes) == (
        SpeedMode.STANDARD,
    )
    assert models["glm-5.2-fast"].supported_efforts == ("off", "high", "max")
    assert models["glm-5.2-fast"].default_effort == "high"


async def test_context_falls_back_to_model_metadata() -> None:
    collector = _collector()
    snapshot = validate_snapshot(await collector.collect(), collector.sources)
    model = next(model for model in snapshot.models if model.canonical_model == "registry-only")

    assert model.context_length is None
    with patch("gobby.llm.model_registry.lookup_context_window", return_value=131_072):
        resolved = resolve_context_window_with_source(
            model.canonical_model,
            provider="droid",
            provider_reported_context_window=model.context_length,
        )

    assert resolved is not None
    assert resolved.value == 131_072
    assert resolved.source == "registry"
