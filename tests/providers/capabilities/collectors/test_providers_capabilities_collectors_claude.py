"""Tests for the public-document Claude capability collector."""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.providers.capabilities.collectors import validate_snapshot
from gobby.providers.capabilities.collectors.claude import (
    EFFORT_DOCS_URL,
    MODEL_CONFIG_URL,
    MODELS_OVERVIEW_URL,
    ClaudeCollector,
    ClaudeSourceError,
)
from gobby.providers.capabilities.models import ReasoningSupport, SourceState, SpeedMode

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parent / "fixtures" / "claude"
_OBSERVED_AT = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
_SOURCE_KEYS = {
    MODELS_OVERVIEW_URL: "models-overview",
    MODEL_CONFIG_URL: "model-config",
    EFFORT_DOCS_URL: "effort-docs",
}


def _documents() -> dict[str, str]:
    return {
        "models-overview": (_FIXTURES / "models-overview.md").read_text(),
        "model-config": (_FIXTURES / "model-config.md").read_text(),
        "effort-docs": (_FIXTURES / "effort-docs.md").read_text(),
    }


def _collector(documents: Mapping[str, str]) -> ClaudeCollector:
    async def fetch_text(url: str) -> str:
        return documents[_SOURCE_KEYS[url]]

    return ClaudeCollector(fetch_text=fetch_text, clock=lambda: _OBSERVED_AT)


@pytest.mark.asyncio
async def test_alias_to_canonical_mapping() -> None:
    snapshot = await _collector(_documents()).collect()
    models = {model.canonical_model: model for model in snapshot.models}

    assert {"opus", "opus[1m]", "opusplan"} <= set(models["claude-opus-5"].aliases)
    assert {"sonnet", "sonnet[1m]"} <= set(models["claude-sonnet-5"].aliases)
    assert "fable" in models["claude-fable-5"].aliases
    assert "haiku" in models["claude-haiku-4-5-20251001"].aliases


@pytest.mark.asyncio
async def test_alias_to_undocumented_model_version_fails_snapshot() -> None:
    documents = _documents()
    documents["model-config"] = documents["model-config"].replace(
        "| Anthropic API | Opus 5 | Sonnet 5 |",
        "| Anthropic API | Opus 4.6 | Sonnet 5 |",
    )

    with pytest.raises(ClaudeSourceError, match="model-config"):
        await _collector(documents).collect()


@pytest.mark.asyncio
async def test_fact_provenance() -> None:
    collector = _collector(_documents())

    snapshot = validate_snapshot(await collector.collect(), collector.sources)
    opus = next(model for model in snapshot.models if model.canonical_model == "claude-opus-5")

    assert opus.context_length == 1_000_000
    assert opus.max_output_tokens == 128_000
    assert opus.latency_class == "moderate"
    for fact in ("context_length", "max_output_tokens", "latency_class"):
        provenance = opus.provenance[fact]
        assert provenance.source_key == "models-overview"
        assert provenance.source_url == MODELS_OVERVIEW_URL
        assert provenance.observed_at == _OBSERVED_AT


@pytest.mark.parametrize(
    ("source_key", "old", "new"),
    [
        ("models-overview", "| Claude API ID |", "| API model |"),
        ("model-config", "| Model alias | Behavior |", "| Name | Behavior |"),
        (
            "effort-docs",
            "| Level | Description | Typical use case |",
            "| Tier | Description | Use |",
        ),
        ("effort-docs", "## Compatibility", "## Model support"),
    ],
)
@pytest.mark.asyncio
async def test_malformed_required_source_fails_snapshot(
    source_key: str,
    old: str,
    new: str,
) -> None:
    documents = _documents()
    documents[source_key] = documents[source_key].replace(old, new)

    with pytest.raises(ClaudeSourceError, match=source_key):
        await _collector(documents).collect()


@pytest.mark.asyncio
async def test_compared_models_emit_standard_routes() -> None:
    collector = _collector(_documents())

    snapshot = validate_snapshot(await collector.collect(), collector.sources)
    models = {model.canonical_model: model for model in snapshot.models}

    assert set(models) == {
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    }
    assert all(
        tuple(route.speed_mode for route in model.routes) == (SpeedMode.STANDARD,)
        for model in models.values()
    )
    assert all(model.routes[0].selector == model.canonical_model for model in models.values())
    assert all(source.state is SourceState.OK for source in snapshot.sources)


@pytest.mark.asyncio
async def test_effort_compatibility_uses_canonical_model_ids() -> None:
    documents = _documents()

    snapshot = await _collector(documents).collect()
    models = {model.canonical_model: model for model in snapshot.models}

    assert models["claude-opus-5"].supported_efforts == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert models["claude-opus-5"].default_effort == "high"
    assert models["claude-fable-5"].supported_efforts == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert models["claude-haiku-4-5-20251001"].supported_efforts is None
    assert models["claude-haiku-4-5-20251001"].reasoning is ReasoningSupport.KNOWN
    assert "claude-mythos-5" not in models


@pytest.mark.parametrize(
    ("replacement", "detail"),
    [
        ("- Compatible models: `claude-opus-5`", "exactly one Supported models"),
        ("- Supported models:", "lists no canonical model IDs"),
        ("- Supported models: claude-opus-5", "lists no canonical model IDs"),
        (
            "- Supported models: `claude-mythos-5`, `claude-mythos-preview`",
            "overlaps no overview models",
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_effort_supported_models_declaration_fails_snapshot(
    replacement: str,
    detail: str,
) -> None:
    documents = _documents()
    declaration = next(
        line for line in documents["effort-docs"].splitlines() if "Supported models:" in line
    )
    documents["effort-docs"] = documents["effort-docs"].replace(declaration, replacement)

    with pytest.raises(ClaudeSourceError, match=detail):
        await _collector(documents).collect()


@pytest.mark.asyncio
async def test_duplicate_effort_supported_models_declaration_fails_snapshot() -> None:
    documents = _documents()
    declaration = next(
        line for line in documents["effort-docs"].splitlines() if "Supported models:" in line
    )
    documents["effort-docs"] += f"\n{declaration}\n"

    with pytest.raises(ClaudeSourceError, match="exactly one Supported models"):
        await _collector(documents).collect()
