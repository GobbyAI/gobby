"""Codex capabilities collected from the local app-server model catalog."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.providers.capabilities.collectors.base import SourceSpec
from gobby.providers.capabilities.models import (
    ActivationDescriptor,
    FactProvenance,
    ModelCapability,
    ModelRoute,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
    SpeedMode,
)

_SOURCE_KEY = "app-server-model-list"
_MODELS_CACHE_SOURCE_KEY = "models-cache"
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
_ROUTE_FACTS = frozenset({"speed_mode", "selector", "available", "activations"})

RawModel = Mapping[str, object]
FetchModels = Callable[[], Awaitable[Sequence[RawModel]]]
FetchModelsCache = Callable[[], Awaitable[Mapping[str, int]]]
Clock = Callable[[], datetime]


class CodexSourceError(ValueError):
    """Raised when local Codex model metadata cannot produce a snapshot."""

    def __init__(self, source_key: str, detail: str) -> None:
        self.source_key = source_key
        super().__init__(f"Codex source {source_key!r} failed: {detail}")


async def _fetch_app_server_models() -> Sequence[RawModel]:
    async with CodexAppServerClient() as client:
        return await client.list_models(include_hidden=True)


async def _fetch_models_cache() -> Mapping[str, int]:
    codex_home = os.environ.get("CODEX_HOME")
    cache_dir = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    cache_path = cache_dir / "models_cache.json"
    try:
        contents = await asyncio.to_thread(cache_path.read_text, encoding="utf-8")
    except OSError as error:
        raise CodexSourceError(
            _MODELS_CACHE_SOURCE_KEY,
            f"could not read {cache_path}: {error}",
        ) from error

    try:
        payload: object = json.loads(contents)
    except json.JSONDecodeError as error:
        raise CodexSourceError(
            _MODELS_CACHE_SOURCE_KEY,
            f"could not parse {cache_path}: {error}",
        ) from error

    try:
        return _parse_models_cache(payload)
    except ValueError as error:
        raise CodexSourceError(
            _MODELS_CACHE_SOURCE_KEY,
            f"invalid {cache_path}: {error}",
        ) from error


def _parse_models_cache(payload: object) -> dict[str, int]:
    if not isinstance(payload, Mapping):
        raise ValueError("top-level value must be an object")
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise ValueError("models must be an array")

    context_lengths: dict[str, int] = {}
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            continue
        slug = raw_model.get("slug")
        context_window = raw_model.get("context_window")
        if not isinstance(slug, str) or not slug.strip():
            continue
        if (
            isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or context_window <= 0
        ):
            continue
        context_lengths[slug.strip()] = context_window
    return context_lengths


@dataclass(frozen=True)
class CodexCollector:
    """Build a complete Codex capability snapshot from local app-server metadata."""

    fetch_models: FetchModels = _fetch_app_server_models
    fetch_models_cache: FetchModelsCache = _fetch_models_cache
    clock: Clock = lambda: datetime.now(UTC)

    provider = "codex"
    sources = (
        SourceSpec(_SOURCE_KEY, None, required=True),
        SourceSpec(_MODELS_CACHE_SOURCE_KEY, None, required=False),
    )

    async def collect(self) -> ProviderSnapshot:
        observed_at = self.clock()
        try:
            raw_models = await self.fetch_models()
        except CodexSourceError:
            raise
        except Exception as error:
            raise CodexSourceError(_SOURCE_KEY, f"model/list failed: {error}") from error
        if not raw_models:
            raise CodexSourceError(_SOURCE_KEY, "model/list returned no models")

        cache_error: str | None = None
        try:
            cache_context_lengths = await self.fetch_models_cache()
        except Exception as error:
            cache_context_lengths = {}
            cache_error = str(error)

        try:
            models = tuple(
                _build_model(raw_model, cache_context_lengths, observed_at, index)
                for index, raw_model in enumerate(raw_models)
            )
        except CodexSourceError:
            raise
        except (TypeError, ValueError) as error:
            raise CodexSourceError(_SOURCE_KEY, str(error)) from error

        return ProviderSnapshot(
            provider=self.provider,
            generation=0,
            models=models,
            sources=(
                SourceHealth(
                    source_key=_SOURCE_KEY,
                    source_url=None,
                    required=True,
                    state=SourceState.OK,
                    attempts=1,
                    last_attempt_at=observed_at,
                    last_success_at=observed_at,
                    last_error=None,
                ),
                SourceHealth(
                    source_key=_MODELS_CACHE_SOURCE_KEY,
                    source_url=None,
                    required=False,
                    state=SourceState.ERROR if cache_error is not None else SourceState.OK,
                    attempts=1,
                    last_attempt_at=observed_at,
                    last_success_at=None if cache_error is not None else observed_at,
                    last_error=cache_error,
                ),
            ),
        )


def _build_model(
    raw: RawModel,
    cache_context_lengths: Mapping[str, int],
    observed_at: datetime,
    index: int,
) -> ModelCapability:
    canonical_model = _required_string(
        _first(raw, "model", "id", "slug"),
        f"model entry {index} id",
    )
    display_name = _required_string(
        _first(raw, "displayName", "display_name"),
        f"model {canonical_model!r} display name",
    )
    aliases = _aliases(raw, canonical_model)
    context_length, context_source = _context_length(
        raw,
        canonical_model,
        cache_context_lengths,
    )
    supported_efforts = _reasoning_efforts(raw, canonical_model)
    default_effort = _optional_string(
        _first(
            raw,
            "defaultReasoningEffort",
            "defaultReasoningMode",
            "default_reasoning_level",
        ),
        f"model {canonical_model!r} default reasoning effort",
    )
    input_modalities = _optional_string_tuple(
        _first(raw, "inputModalities", "input_modalities"),
        f"model {canonical_model!r} input modalities",
    )
    fast_tier = _fast_tier(raw, canonical_model)

    reasoning = ReasoningSupport.UNKNOWN
    if supported_efforts == () and default_effort is None:
        reasoning = ReasoningSupport.UNSUPPORTED
    elif supported_efforts or default_effort is not None:
        reasoning = ReasoningSupport.KNOWN

    routes = [_route(canonical_model, SpeedMode.STANDARD, observed_at)]
    if fast_tier is not None:
        routes.append(_route(canonical_model, SpeedMode.FAST, observed_at, fast_tier))

    model_facts = set(_MODEL_BASE_FACTS)
    if context_length is not None:
        model_facts.add("context_length")
    if supported_efforts is not None:
        model_facts.add("supported_efforts")
    if default_effort is not None:
        model_facts.add("default_effort")
    if input_modalities is not None:
        model_facts.add("input_modalities")

    provenance = _provenance(model_facts, observed_at)
    if context_source == _MODELS_CACHE_SOURCE_KEY:
        provenance["context_length"] = FactProvenance(
            source_key=_MODELS_CACHE_SOURCE_KEY,
            source_url=None,
            observed_at=observed_at,
        )

    return ModelCapability(
        canonical_model=canonical_model,
        display_name=display_name,
        aliases=aliases,
        available=True,
        hidden=_optional_bool(_first(raw, "hidden"), default=False),
        is_default=_optional_bool(
            _first(raw, "isDefault", "is_default"),
            default=False,
        ),
        context_length=context_length,
        max_output_tokens=None,
        reasoning=reasoning,
        supported_efforts=supported_efforts,
        default_effort=default_effort,
        latency_class=None,
        input_modalities=input_modalities,
        supports_tools=None,
        routes=tuple(routes),
        provenance=provenance,
    )


def _route(
    selector: str,
    speed_mode: SpeedMode,
    observed_at: datetime,
    fast_tier: str | None = None,
) -> ModelRoute:
    activations: tuple[ActivationDescriptor, ...] = ()
    if fast_tier is not None:
        activations = (
            ActivationDescriptor(
                kind="request_parameter",
                surface="app-server",
                params={"name": "serviceTier", "value": fast_tier},
            ),
        )
    return ModelRoute(
        speed_mode=speed_mode,
        selector=selector,
        available=True,
        usage_multiplier=None,
        throughput_multiplier=None,
        latency_class=None,
        activations=activations,
        provenance=_provenance(_ROUTE_FACTS, observed_at),
    )


def _aliases(raw: RawModel, canonical_model: str) -> tuple[str, ...]:
    aliases: list[str] = []
    for key in ("id", "slug"):
        alias = _optional_string(raw.get(key), f"model {canonical_model!r} alias")
        if alias is not None and alias != canonical_model and alias not in aliases:
            aliases.append(alias)
    return tuple(aliases)


def _context_length(
    raw: RawModel,
    canonical_model: str,
    cache_context_lengths: Mapping[str, int],
) -> tuple[int | None, str | None]:
    value = _first(
        raw,
        "contextWindow",
        "context_window",
        "contextLength",
        "context_length",
    )
    if value is None:
        value = _first(raw, "maxContextWindow", "max_context_window")
    if value is None:
        cached_value = cache_context_lengths.get(canonical_model)
        if cached_value is None:
            return None, None
        return cached_value, _MODELS_CACHE_SOURCE_KEY
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"model {canonical_model!r} context window must be a positive integer")
    return value, _SOURCE_KEY


def _reasoning_efforts(raw: RawModel, canonical_model: str) -> tuple[str, ...] | None:
    value = _first(
        raw,
        "supportedReasoningEfforts",
        "supported_reasoning_levels",
        "reasoningEfforts",
    )
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"model {canonical_model!r} reasoning efforts must be a list")

    efforts: list[str] = []
    for item in value:
        raw_effort = item
        if isinstance(item, Mapping):
            raw_effort = _first(item, "reasoningEffort", "reasoning_effort", "effort")
        effort = _required_string(
            raw_effort,
            f"model {canonical_model!r} reasoning effort",
        )
        if effort not in efforts:
            efforts.append(effort)
    return tuple(efforts)


def _fast_tier(raw: RawModel, canonical_model: str) -> str | None:
    service_tiers = _first(raw, "serviceTiers", "service_tiers")
    if service_tiers is not None:
        if isinstance(service_tiers, (str, bytes)) or not isinstance(service_tiers, Sequence):
            raise ValueError(f"model {canonical_model!r} service tiers must be a list")
        for item in service_tiers:
            if not isinstance(item, Mapping):
                raise ValueError(f"model {canonical_model!r} service tier must be an object")
            tier_id = _optional_string(item.get("id"), "service tier id")
            tier_name = _optional_string(item.get("name"), "service tier name")
            if tier_id is not None and (
                tier_id.casefold() == "fast"
                or (tier_name is not None and tier_name.casefold() == "fast")
            ):
                return tier_id

    additional_tiers = _first(raw, "additionalSpeedTiers", "additional_speed_tiers")
    if additional_tiers is None:
        return None
    tiers = _optional_string_tuple(
        additional_tiers,
        f"model {canonical_model!r} additional speed tiers",
    )
    assert tiers is not None
    return next((tier for tier in tiers if tier.casefold() == "fast"), None)


def _optional_string_tuple(value: object, field: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be a list")
    return tuple(_required_string(item, field) for item in value)


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


def _first(mapping: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _provenance(
    facts: Sequence[str] | set[str] | frozenset[str], observed_at: datetime
) -> dict[str, FactProvenance]:
    return {
        fact: FactProvenance(
            source_key=_SOURCE_KEY,
            source_url=None,
            observed_at=observed_at,
        )
        for fact in facts
    }
