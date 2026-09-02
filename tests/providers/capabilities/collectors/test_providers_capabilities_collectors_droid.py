from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from gobby.llm.context_windows import resolve_context_window_with_source
from gobby.providers.capabilities.collectors import validate_snapshot
from gobby.providers.capabilities.collectors.droid import (
    DROID_MODELS_URL,
    DroidCollector,
)
from gobby.providers.capabilities.models import ReasoningSupport

pytestmark = pytest.mark.unit

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


@pytest.mark.asyncio
async def test_model_row_facts_parsed() -> None:
    collector = _collector()
    snapshot = validate_snapshot(await collector.collect(), collector.sources)

    model = next(model for model in snapshot.models if model.canonical_model == "gpt-5.5")

    assert model.supported_efforts == ("none", "low", "medium", "high", "xhigh")
    assert model.default_effort == "medium"
    assert model.reasoning is ReasoningSupport.KNOWN


@pytest.mark.asyncio
async def test_fast_models_are_ordinary_selectable_models() -> None:
    """`-fast` ids are plain Droid models, never folded into their base model."""
    collector = _collector()
    snapshot = validate_snapshot(await collector.collect(), collector.sources)
    models = {model.canonical_model: model for model in snapshot.models}

    assert set(models) == {
        "gpt-5.5",
        "gpt-5.5-fast",
        "suffix-only-fast",
        "glm-5.2-fast",
        "registry-only",
    }
    assert models["gpt-5.5-fast"].display_name == "GPT-5.5 Fast"
    assert models["gpt-5.5-fast"].supported_efforts == (
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
    )
    assert models["suffix-only-fast"].supported_efforts == ("minimal", "high")
    assert models["suffix-only-fast"].default_effort == "minimal"
    assert models["glm-5.2-fast"].supported_efforts == ("off", "high", "max")
    assert models["glm-5.2-fast"].default_effort == "high"


@pytest.mark.asyncio
async def test_row_with_unparsable_usage_multiplier_is_rejected() -> None:
    document = _MODELS_DOCUMENT.replace("| 2\u00d7 |", "| free |")
    collector = _collector(document)

    with pytest.raises(ValueError, match="invalid usage multiplier for 'gpt-5.5'"):
        await collector.collect()


@pytest.mark.asyncio
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
