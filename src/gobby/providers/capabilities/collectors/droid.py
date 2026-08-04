"""Droid capability collector backed by Factory's public model table."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx

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

DROID_MODELS_URL = "https://docs.factory.ai/models.md"

_SOURCE_KEY = "factory-models"
_MODEL_ID_RE = re.compile(r"`([^`]+)`")
_EFFORT_RE = re.compile(r"`([^`]+)`(\s*\(default\))?", re.IGNORECASE)
_EXPLICIT_FAST_RE = re.compile(r"\bfast(?:\s+mode)?\b", re.IGNORECASE)
_MULTIPLIER_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*[×x]", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

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
_ROUTE_BASE_FACTS = frozenset({"speed_mode", "selector", "available", "activations"})

FetchText = Callable[[str], Awaitable[str]]
Clock = Callable[[], datetime]


class DroidSourceError(ValueError):
    """Raised when Factory's model source cannot produce a safe snapshot."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"{_SOURCE_KEY}: {detail}")


@dataclass(frozen=True)
class _DroidModel:
    display_name: str
    model_id: str
    usage_multiplier: Decimal
    supported_efforts: tuple[str, ...] | None
    default_effort: str | None


async def _fetch_factory_models(url: str) -> str:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


@dataclass(frozen=True)
class DroidCollector:
    """Build a Droid capability snapshot from Factory's public documentation."""

    fetch_text: FetchText = _fetch_factory_models
    clock: Clock = lambda: datetime.now(UTC)

    provider = "droid"
    sources = (SourceSpec(_SOURCE_KEY, DROID_MODELS_URL, required=True),)

    async def collect(self) -> ProviderSnapshot:
        observed_at = self.clock()
        document = await self._fetch_document()
        try:
            parsed_models = _parse_models(document)
        except ValueError as error:
            raise DroidSourceError(str(error)) from error

        by_id = {model.model_id: model for model in parsed_models}
        fast_by_standard = {
            model.model_id.removesuffix("-fast"): model
            for model in parsed_models
            if _is_pairable_fast(model, by_id)
        }
        paired_fast_ids = {model.model_id for model in fast_by_standard.values()}
        models = tuple(
            _build_model(model, fast_by_standard.get(model.model_id), observed_at)
            for model in parsed_models
            if model.model_id not in paired_fast_ids
        )
        return ProviderSnapshot(
            provider=self.provider,
            generation=0,
            models=models,
            sources=(_healthy_source(self.sources[0], observed_at),),
        )

    async def _fetch_document(self) -> str:
        source = self.sources[0]
        if source.url is None:
            raise DroidSourceError("source URL is missing")
        try:
            document = await self.fetch_text(source.url)
        except Exception as error:
            raise DroidSourceError(f"fetch failed: {error}") from error
        if not document.strip():
            raise DroidSourceError("response was empty")
        return document


def _parse_models(document: str) -> tuple[_DroidModel, ...]:
    models: list[_DroidModel] = []
    seen_ids: set[str] = set()
    for line in document.splitlines():
        cells = _model_row_cells(line)
        if cells is None:
            continue
        label_cell, id_cell, multiplier_cell, reasoning_cell = cells[:4]
        id_match = _MODEL_ID_RE.fullmatch(id_cell)
        if id_match is None:
            continue
        model_id = id_match.group(1).strip()
        if not model_id:
            raise ValueError("model ID is empty")
        if model_id in seen_ids:
            raise ValueError(f"duplicate model ID {model_id!r}")
        seen_ids.add(model_id)
        efforts, default_effort = _parse_reasoning(reasoning_cell)
        models.append(
            _DroidModel(
                display_name=_clean_label(label_cell) or model_id,
                model_id=model_id,
                usage_multiplier=_parse_multiplier(multiplier_cell, model_id),
                supported_efforts=efforts,
                default_effort=default_effort,
            )
        )
    if not models:
        raise ValueError("no model table rows parsed from Factory docs models page")
    return tuple(models)


def _model_row_cells(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    cells = tuple(cell.strip() for cell in stripped.strip("|").split("|"))
    return cells if len(cells) >= 4 else None


def _parse_multiplier(value: str, model_id: str) -> Decimal:
    cleaned = _HTML_TAG_RE.sub("", value).replace("`", "").strip()
    match = _MULTIPLIER_RE.fullmatch(cleaned)
    if match is None:
        raise ValueError(f"invalid usage multiplier for {model_id!r}: {value!r}")
    return Decimal(match.group(1))


def _parse_reasoning(value: str) -> tuple[tuple[str, ...] | None, str | None]:
    matches = _EFFORT_RE.findall(value)
    if not matches:
        return None, None
    efforts = tuple(effort.strip().lower() for effort, _default in matches)
    defaults = tuple(effort.strip().lower() for effort, default_marker in matches if default_marker)
    if len(defaults) > 1:
        raise ValueError("reasoning cell contains multiple default efforts")
    return efforts, defaults[0] if defaults else efforts[0]


def _is_pairable_fast(model: _DroidModel, by_id: Mapping[str, _DroidModel]) -> bool:
    if not model.model_id.endswith("-fast"):
        return False
    standard_id = model.model_id.removesuffix("-fast")
    return bool(
        standard_id and _EXPLICIT_FAST_RE.search(model.display_name) and standard_id in by_id
    )


def _build_model(
    model: _DroidModel,
    fast_model: _DroidModel | None,
    observed_at: datetime,
) -> ModelCapability:
    routes = [_build_route(model, SpeedMode.STANDARD, observed_at)]
    if fast_model is not None:
        routes.append(_build_route(fast_model, SpeedMode.FAST, observed_at))

    fact_sources = dict.fromkeys(_MODEL_BASE_FACTS, _SOURCE_KEY)
    if model.supported_efforts is not None:
        fact_sources["supported_efforts"] = _SOURCE_KEY
        fact_sources["default_effort"] = _SOURCE_KEY
    return ModelCapability(
        canonical_model=model.model_id,
        display_name=model.display_name,
        aliases=(),
        available=True,
        hidden=False,
        is_default=False,
        context_length=None,
        max_output_tokens=None,
        reasoning=(
            ReasoningSupport.KNOWN
            if model.supported_efforts is not None
            else ReasoningSupport.UNSUPPORTED
        ),
        supported_efforts=model.supported_efforts,
        default_effort=model.default_effort,
        latency_class=None,
        input_modalities=None,
        supports_tools=None,
        routes=tuple(routes),
        provenance=_provenance(fact_sources, observed_at),
    )


def _build_route(model: _DroidModel, speed_mode: SpeedMode, observed_at: datetime) -> ModelRoute:
    activations: tuple[ActivationDescriptor, ...] = ()
    if speed_mode is SpeedMode.FAST:
        activations = tuple(
            ActivationDescriptor(kind="model_selector", surface=surface, params={})
            for surface in ("spawn-cli", "tool-chat")
        )
    fact_sources = dict.fromkeys(_ROUTE_BASE_FACTS, _SOURCE_KEY)
    fact_sources["usage_multiplier"] = _SOURCE_KEY
    return ModelRoute(
        speed_mode=speed_mode,
        selector=model.model_id,
        available=True,
        usage_multiplier=model.usage_multiplier,
        throughput_multiplier=None,
        latency_class=None,
        activations=activations,
        provenance=_provenance(fact_sources, observed_at),
    )


def _provenance(
    fact_sources: Mapping[str, str],
    observed_at: datetime,
) -> dict[str, FactProvenance]:
    return {
        fact: FactProvenance(
            source_key=source_key,
            source_url=DROID_MODELS_URL,
            observed_at=observed_at,
        )
        for fact, source_key in fact_sources.items()
    }


def _healthy_source(source: SourceSpec, observed_at: datetime) -> SourceHealth:
    return SourceHealth(
        source_key=source.source_key,
        source_url=source.url,
        required=source.required,
        state=SourceState.OK,
        attempts=1,
        last_attempt_at=observed_at,
        last_success_at=observed_at,
        last_error=None,
    )


def _clean_label(value: str) -> str:
    return _HTML_TAG_RE.sub("", value).strip()
