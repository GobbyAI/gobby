"""Grok capabilities collected from local ACP model discovery."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gobby.adapters.grok_acp_client import GrokACPClient
from gobby.agents.trust import authorize_model_discovery_trust
from gobby.paths import get_gobby_home
from gobby.providers.capabilities.collectors.base import SourceSpec
from gobby.providers.capabilities.models import (
    FactProvenance,
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
)
from gobby.servers.provider_model_discovery import discover_acp_models
from gobby.servers.provider_models_grok import models_from_cache

logger = logging.getLogger(__name__)

_SOURCE_KEY = "local-model-discovery"
_EFFORT_SOURCE_KEY = "xai-reasoning-docs"
_EFFORT_SOURCE_URL = "https://docs.x.ai/developers/model-capabilities/text/reasoning"
_DOCUMENTED_EFFORTS: dict[str, tuple[tuple[str, ...], str]] = {
    "grok-4.7": (("low", "medium", "high", "xhigh"), "high"),
    "grok-4.6": (("low", "medium", "high", "xhigh"), "high"),
    "grok-4.5": (("low", "medium", "high"), "high"),
}
_MODELS_CACHE_SOURCE_KEY = "models-cache"
_MODEL_DISCOVERY_CWD_NAME = "provider-model-discovery"
_MODEL_BASE_FACTS = frozenset(
    {
        "canonical_model",
        "display_name",
        "aliases",
        "available",
        "hidden",
        "is_default",
        "reasoning",
    }
)

type RawModel = Mapping[str, object]
type DiscoverModels = Callable[[], Awaitable[Sequence[RawModel]]]
type FetchModelsCache = Callable[[], Awaitable[Sequence[RawModel]]]
type Clock = Callable[[], datetime]


class GrokSourceError(ValueError):
    """Raised when local Grok discovery cannot produce a snapshot."""

    def __init__(self, detail: str) -> None:
        self.source_key = _SOURCE_KEY
        super().__init__(f"Grok source {_SOURCE_KEY!r} failed: {detail}")


async def _model_discovery_cwd(provider: str) -> tuple[Path, bool]:
    cwd = get_gobby_home() / _MODEL_DISCOVERY_CWD_NAME / provider
    created = False
    try:
        await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=False)
        created = True
    except FileExistsError:
        if not await asyncio.to_thread(cwd.is_dir):
            raise
    return cwd.resolve(), created


async def _discover_grok_models() -> Sequence[RawModel]:
    return await discover_acp_models(
        client_cls=GrokACPClient,
        which=shutil.which,
        model_discovery_cwd=_model_discovery_cwd,
        authorize_trust=authorize_model_discovery_trust,
        cleanup_tree=shutil.rmtree,
        logger=logger,
    )


async def _fetch_models_cache() -> Sequence[RawModel]:
    return models_from_cache()


def _merge_cache_models(
    raw_models: Sequence[RawModel],
    cache_entries: Sequence[RawModel],
) -> tuple[dict[str, int], Sequence[RawModel]]:
    windows: dict[str, int] = {}
    extras: list[RawModel] = []
    seen = {
        str(model.get("value"))
        for model in raw_models
        if isinstance(model.get("value"), str) and str(model.get("value"))
    }
    for entry in cache_entries:
        value = entry.get("value")
        if not isinstance(value, str) or not value:
            continue
        length = entry.get("context_length")
        if isinstance(length, int) and not isinstance(length, bool) and length > 0:
            windows[value] = length
        if value not in seen:
            extras.append(
                {
                    "value": value,
                    "label": entry.get("label") or value,
                }
            )
            seen.add(value)
    if not extras:
        return windows, raw_models
    return windows, tuple(raw_models) + tuple(extras)


@dataclass(frozen=True)
class GrokCollector:
    """Build a Grok capability snapshot from local ACP metadata."""

    discover_models: DiscoverModels = _discover_grok_models
    fetch_models_cache: FetchModelsCache = _fetch_models_cache
    clock: Clock = lambda: datetime.now(UTC)

    provider = "grok"
    sources = (
        SourceSpec(_SOURCE_KEY, None, required=True),
        SourceSpec(_MODELS_CACHE_SOURCE_KEY, None, required=False),
        SourceSpec(_EFFORT_SOURCE_KEY, _EFFORT_SOURCE_URL, required=False),
    )

    async def collect(self) -> ProviderSnapshot:
        observed_at = self.clock()
        try:
            raw_models = await self.discover_models()
        except GrokSourceError:
            raise
        except Exception as error:
            raise GrokSourceError(str(error)) from error
        if not raw_models:
            raise GrokSourceError("discovery returned no models")

        cache_error: str | None = None
        try:
            cache_entries = tuple(await self.fetch_models_cache())
        except Exception as error:
            cache_entries = ()
            cache_error = str(error)

        cache_windows, merged_models = _merge_cache_models(raw_models, cache_entries)

        try:
            models = tuple(
                _build_model(raw_model, observed_at, index, cache_windows)
                for index, raw_model in enumerate(merged_models)
            )
        except GrokSourceError:
            raise
        except (TypeError, ValueError) as error:
            raise GrokSourceError(str(error)) from error

        return ProviderSnapshot(
            provider=self.provider,
            generation=0,
            models=models,
            sources=(
                _healthy_source(observed_at),
                _cache_source(observed_at, cache_error),
                _effort_source(observed_at),
            ),
        )


def _build_model(
    raw: RawModel,
    observed_at: datetime,
    index: int,
    cache_windows: Mapping[str, int],
) -> ModelCapability:
    canonical_model = _required_string(raw.get("value"), f"model entry {index} id")
    display_name = _optional_string(raw.get("label"), f"model {canonical_model!r} label")
    context_length, context_source = _context_length(
        raw.get("context_length"), canonical_model, cache_windows
    )
    reasoning, supported_efforts, default_effort = _reasoning(raw.get("reasoning"), canonical_model)

    model_facts = set(_MODEL_BASE_FACTS)
    fact_sources: dict[str, str] = {}
    fact_urls: dict[str, str] = {}
    documented = _documented_reasoning(canonical_model)
    if supported_efforts is None and default_effort is None and documented is not None:
        supported_efforts, default_effort = documented
        reasoning = ReasoningSupport.KNOWN
        for fact in ("reasoning", "supported_efforts", "default_effort"):
            fact_sources[fact] = _EFFORT_SOURCE_KEY
            fact_urls[fact] = _EFFORT_SOURCE_URL
    if context_length is not None:
        model_facts.add("context_length")
        fact_sources["context_length"] = context_source
    if supported_efforts is not None:
        model_facts.add("supported_efforts")
    if default_effort is not None:
        model_facts.add("default_effort")

    return ModelCapability(
        canonical_model=canonical_model,
        display_name=display_name or canonical_model,
        aliases=(),
        available=True,
        hidden=False,
        is_default=_optional_bool(raw.get("is_default"), default=False),
        context_length=context_length,
        max_output_tokens=None,
        reasoning=reasoning,
        supported_efforts=supported_efforts,
        default_effort=default_effort,
        latency_class=None,
        input_modalities=None,
        supports_tools=None,
        provenance=_provenance(
            model_facts,
            observed_at,
            fact_sources=fact_sources,
            fact_urls=fact_urls,
        ),
    )


def _reasoning(
    value: object,
    canonical_model: str,
) -> tuple[ReasoningSupport, tuple[str, ...] | None, str | None]:
    if value is None:
        return ReasoningSupport.UNKNOWN, None, None
    if not isinstance(value, Mapping):
        raise ValueError(f"model {canonical_model!r} reasoning must be an object")

    raw_efforts = value.get("supported_efforts")
    supported_efforts: tuple[str, ...] | None = None
    if raw_efforts is not None:
        if isinstance(raw_efforts, (str, bytes)) or not isinstance(raw_efforts, Sequence):
            raise ValueError(f"model {canonical_model!r} reasoning efforts must be a list")
        supported_efforts = tuple(
            _required_string(item, f"model {canonical_model!r} reasoning effort")
            for item in raw_efforts
        )
    default_effort = _optional_string(
        value.get("default_effort"),
        f"model {canonical_model!r} default reasoning effort",
    )
    if supported_efforts is None and default_effort is None:
        return ReasoningSupport.UNKNOWN, None, None
    if supported_efforts == () and default_effort is None:
        return ReasoningSupport.UNSUPPORTED, supported_efforts, None
    return ReasoningSupport.KNOWN, supported_efforts, default_effort


def _context_length(
    value: object,
    canonical_model: str,
    cache_windows: Mapping[str, int],
) -> tuple[int | None, str]:
    if value is None:
        cached = cache_windows.get(canonical_model)
        if cached is None:
            return None, _SOURCE_KEY
        return cached, _MODELS_CACHE_SOURCE_KEY
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"model {canonical_model!r} context length must be a positive integer")
    return value, _SOURCE_KEY


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field)


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field} must be a non-empty normalized string")
    return value


def _optional_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("boolean model facts must be booleans")
    return value


def _documented_reasoning(canonical_model: str) -> tuple[tuple[str, ...], str] | None:
    """Effort table from the xAI reasoning docs when discovery omits it."""
    for prefix, efforts in _DOCUMENTED_EFFORTS.items():
        if canonical_model == prefix or canonical_model.startswith(f"{prefix}-"):
            return efforts
    return None


def _provenance(
    facts: Sequence[str] | set[str] | frozenset[str],
    observed_at: datetime,
    *,
    fact_sources: Mapping[str, str] | None = None,
    fact_urls: Mapping[str, str] | None = None,
) -> dict[str, FactProvenance]:
    sources = fact_sources or {}
    urls = fact_urls or {}
    return {
        fact: FactProvenance(
            source_key=sources.get(fact, _SOURCE_KEY),
            source_url=urls.get(fact),
            observed_at=observed_at,
        )
        for fact in facts
    }


def _effort_source(observed_at: datetime) -> SourceHealth:
    return SourceHealth(
        source_key=_EFFORT_SOURCE_KEY,
        source_url=_EFFORT_SOURCE_URL,
        required=False,
        state=SourceState.OK,
        attempts=1,
        last_attempt_at=observed_at,
        last_success_at=observed_at,
        last_error=None,
    )


def _healthy_source(observed_at: datetime) -> SourceHealth:
    return SourceHealth(
        source_key=_SOURCE_KEY,
        source_url=None,
        required=True,
        state=SourceState.OK,
        attempts=1,
        last_attempt_at=observed_at,
        last_success_at=observed_at,
        last_error=None,
    )


def _cache_source(observed_at: datetime, error: str | None) -> SourceHealth:
    return SourceHealth(
        source_key=_MODELS_CACHE_SOURCE_KEY,
        source_url=None,
        required=False,
        state=SourceState.ERROR if error is not None else SourceState.OK,
        attempts=1,
        last_attempt_at=observed_at,
        last_success_at=None if error is not None else observed_at,
        last_error=error,
    )
