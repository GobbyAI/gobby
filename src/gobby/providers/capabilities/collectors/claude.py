"""Claude capabilities collected from Anthropic's public Markdown documentation."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from gobby.providers.capabilities.collectors.base import SourceSpec
from gobby.providers.capabilities.models import (
    FactProvenance,
    ModelCapability,
    ModelRoute,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
    SpeedMode,
)

MODELS_OVERVIEW_URL = "https://platform.claude.com/docs/en/about-claude/models/overview.md"
MODEL_CONFIG_URL = "https://code.claude.com/docs/en/model-config.md"
EFFORT_DOCS_URL = "https://platform.claude.com/docs/en/build-with-claude/effort.md"

_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
_REQUIRED_ALIASES = frozenset(
    {"fable", "opus", "sonnet", "haiku", "sonnet[1m]", "opus[1m]", "opusplan"}
)
_LATENCY_CLASSES = frozenset({"slower", "moderate", "fast", "fastest"})
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


class ClaudeSourceError(ValueError):
    """Raised when one required Claude documentation source cannot produce facts."""

    def __init__(self, source_key: str, detail: str) -> None:
        self.source_key = source_key
        super().__init__(f"Claude source {source_key!r} failed: {detail}")


@dataclass(frozen=True)
class _MarkdownTable:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class _OverviewModel:
    canonical_model: str
    display_name: str
    api_alias: str
    context_length: int
    max_output_tokens: int
    latency_class: str
    extended_thinking: bool
    adaptive_thinking: bool


@dataclass(frozen=True)
class _EffortSupport:
    levels: tuple[str, ...] | None
    default: str | None


async def _fetch_public_markdown(url: str) -> str:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


@dataclass(frozen=True)
class ClaudeCollector:
    """Build a complete Claude capability snapshot from public documentation."""

    fetch_text: FetchText = _fetch_public_markdown
    clock: Clock = lambda: datetime.now(UTC)

    provider = "claude"
    sources = (
        SourceSpec("models-overview", MODELS_OVERVIEW_URL, required=True),
        SourceSpec("model-config", MODEL_CONFIG_URL, required=True),
        SourceSpec("effort-docs", EFFORT_DOCS_URL, required=True),
    )

    async def collect(self) -> ProviderSnapshot:
        observed_at = self.clock()
        documents = await self._fetch_documents()
        overview_models = _from_source(
            "models-overview",
            lambda: _parse_models_overview(documents["models-overview"]),
        )
        code_aliases = _from_source(
            "model-config",
            lambda: _parse_model_config(documents["model-config"], overview_models),
        )
        effort = _from_source(
            "effort-docs",
            lambda: _parse_effort_docs(documents["effort-docs"], overview_models),
        )
        models = tuple(
            _build_model(model, code_aliases.get(model.canonical_model, ()), effort, observed_at)
            for model in overview_models
        )
        return ProviderSnapshot(
            provider=self.provider,
            generation=0,
            models=models,
            sources=tuple(_healthy_source(source, observed_at) for source in self.sources),
        )

    async def _fetch_documents(self) -> dict[str, str]:
        async def fetch(source: SourceSpec) -> tuple[str, str]:
            if source.url is None:
                raise ClaudeSourceError(source.source_key, "source URL is missing")
            try:
                document = await self.fetch_text(source.url)
            except Exception as error:
                raise ClaudeSourceError(source.source_key, f"fetch failed: {error}") from error
            if not document.strip():
                raise ClaudeSourceError(source.source_key, "response was empty")
            return source.source_key, document

        results = await asyncio.gather(*(fetch(source) for source in self.sources))
        return dict(results)


def _from_source[T](source_key: str, parse: Callable[[], T]) -> T:
    try:
        return parse()
    except ClaudeSourceError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise ClaudeSourceError(source_key, str(error)) from error


def _parse_models_overview(document: str) -> tuple[_OverviewModel, ...]:
    section = _required_heading_section(document, "Compare models")
    models = _parse_overview_table(_find_table(section, ("feature",)))
    canonical_ids = [model.canonical_model for model in models]
    if len(canonical_ids) != len(set(canonical_ids)):
        raise ValueError("models overview contains duplicate Claude API IDs")
    return models


def _parse_overview_table(table: _MarkdownTable) -> tuple[_OverviewModel, ...]:
    if len(table.headers) < 2:
        raise ValueError("models overview table has no model columns")
    features = {_normalize_cell(row[0]): row[1:] for row in table.rows}
    api_ids = _required_feature(features, "claude api id")
    api_aliases = _required_feature(features, "claude api alias")
    thinking = _required_feature(features, "thinking", prefix=True)
    latency = _required_feature(features, "comparative latency")
    contexts = _required_feature(features, "context window")
    outputs = _required_feature(features, "max output")
    model_count = len(table.headers) - 1
    required_rows = (api_ids, api_aliases, thinking, latency, contexts, outputs)
    if any(len(row) != model_count for row in required_rows):
        raise ValueError("models overview table has inconsistent model columns")

    models: list[_OverviewModel] = []
    for index, display_cell in enumerate(table.headers[1:]):
        latency_class = _clean_cell(latency[index]).casefold()
        if latency_class not in _LATENCY_CLASSES:
            raise ValueError(f"unknown comparative latency {latency_class!r}")
        thinking_mode = _normalize_cell(thinking[index])
        if not thinking_mode.startswith(("adaptive", "extended")):
            raise ValueError(f"unknown thinking mode {thinking_mode!r}")
        models.append(
            _OverviewModel(
                canonical_model=_clean_cell(api_ids[index]),
                display_name=_clean_cell(display_cell),
                api_alias=_clean_cell(api_aliases[index]),
                context_length=_parse_token_count(contexts[index]),
                max_output_tokens=_parse_token_count(outputs[index]),
                latency_class=latency_class,
                extended_thinking=thinking_mode.startswith("extended"),
                adaptive_thinking=thinking_mode.startswith("adaptive"),
            )
        )
    if any(not model.canonical_model.startswith("claude-") for model in models):
        raise ValueError("models overview contains an invalid Claude API ID")
    return tuple(models)


def _parse_model_config(
    document: str,
    models: Sequence[_OverviewModel],
) -> dict[str, tuple[str, ...]]:
    alias_table = _find_table(document, ("model alias", "behavior"))
    behaviors = {_clean_cell(row[0]): _clean_cell(row[1]) for row in alias_table.rows}
    missing = _REQUIRED_ALIASES - behaviors.keys()
    if missing:
        raise ValueError(f"model alias table is missing: {', '.join(sorted(missing))}")

    provider_table = _find_table(document, ("provider", "opus", "sonnet"))
    anthropic_row = next(
        (row for row in provider_table.rows if "anthropic api" in _normalize_cell(row[0])),
        None,
    )
    if anthropic_row is None or len(anthropic_row) != len(provider_table.headers):
        raise ValueError("provider alias table is missing the Anthropic API row")
    header_indexes = {
        _normalize_cell(header): index for index, header in enumerate(provider_table.headers)
    }
    opus_model = _model_for_version(models, _clean_cell(anthropic_row[header_indexes["opus"]]))
    sonnet_model = _model_for_version(models, _clean_cell(anthropic_row[header_indexes["sonnet"]]))
    fable_model = _model_from_behavior(models, behaviors["fable"], family="fable")
    haiku_model = _model_from_behavior(models, behaviors["haiku"], family="haiku")

    targets = {
        "fable": fable_model.canonical_model,
        "opus": opus_model.canonical_model,
        "opus[1m]": opus_model.canonical_model,
        "opusplan": opus_model.canonical_model,
        "sonnet": sonnet_model.canonical_model,
        "sonnet[1m]": sonnet_model.canonical_model,
        "haiku": haiku_model.canonical_model,
    }
    aliases: dict[str, list[str]] = {}
    for alias, canonical_model in targets.items():
        aliases.setdefault(canonical_model, []).append(alias)
    return {canonical_model: tuple(values) for canonical_model, values in aliases.items()}


def _parse_effort_docs(
    document: str,
    models: Sequence[_OverviewModel],
) -> dict[str, _EffortSupport]:
    compatibility = _required_heading_section(document, "Compatibility")
    declarations = re.findall(
        r"^[ \t]*-\s+Supported models:[ \t]*(.*?)[ \t]*$",
        compatibility,
        flags=re.MULTILINE,
    )
    if len(declarations) != 1:
        raise ValueError(
            "effort compatibility must contain exactly one Supported models declaration"
        )
    documented_model_ids = frozenset(re.findall(r"`(claude-[a-z0-9-]+)`", declarations[0]))
    if not documented_model_ids:
        raise ValueError("effort Supported models declaration lists no canonical model IDs")
    supported_models = tuple(
        model for model in models if model.canonical_model in documented_model_ids
    )
    if not supported_models:
        raise ValueError("effort Supported models declaration overlaps no overview models")

    table = _find_table(document, ("level", "description", "typical use case"))
    levels_by_model: dict[str, set[str]] = {
        model.canonical_model: set() for model in supported_models
    }
    for row in table.rows:
        level = _clean_cell(row[0]).casefold()
        if not level:
            raise ValueError("effort table contains an empty level")
        description = _clean_cell(row[1])
        targets = (
            _models_named_in_text(supported_models, description)
            if "available on" in description.casefold()
            else supported_models
        )
        for model in targets:
            levels_by_model[model.canonical_model].add(level)

    support: dict[str, _EffortSupport] = {}
    for model in models:
        levels = levels_by_model.get(model.canonical_model)
        if levels is None:
            support[model.canonical_model] = _EffortSupport(None, None)
            continue
        if "high" not in levels:
            raise ValueError(f"effort table omits default high level for {model.display_name}")
        ordered = tuple(level for level in _EFFORT_ORDER if level in levels)
        unknown = levels - set(_EFFORT_ORDER)
        ordered += tuple(sorted(unknown))
        support[model.canonical_model] = _EffortSupport(ordered, "high")
    return support


def _build_model(
    model: _OverviewModel,
    code_aliases: tuple[str, ...],
    effort: Mapping[str, _EffortSupport],
    observed_at: datetime,
) -> ModelCapability:
    effort_support = effort[model.canonical_model]
    aliases = tuple(dict.fromkeys((model.api_alias, *code_aliases)))
    reasoning = (
        ReasoningSupport.KNOWN
        if model.extended_thinking or model.adaptive_thinking or effort_support.levels
        else ReasoningSupport.UNSUPPORTED
    )
    model_sources = dict.fromkeys(_MODEL_BASE_FACTS, "models-overview")
    model_sources["aliases"] = "model-config" if code_aliases else "models-overview"
    model_sources["is_default"] = "model-config"
    model_sources.update(
        {
            "context_length": "models-overview",
            "max_output_tokens": "models-overview",
            "latency_class": "models-overview",
            "input_modalities": "models-overview",
        }
    )
    if effort_support.levels is not None:
        model_sources["supported_efforts"] = "effort-docs"
        model_sources["default_effort"] = "effort-docs"

    route_sources = dict.fromkeys(_ROUTE_BASE_FACTS, "models-overview")
    route_sources["latency_class"] = "models-overview"
    route = ModelRoute(
        speed_mode=SpeedMode.STANDARD,
        selector=model.canonical_model,
        available=True,
        usage_multiplier=None,
        throughput_multiplier=None,
        latency_class=model.latency_class,
        activations=(),
        provenance=_provenance(route_sources, observed_at),
    )
    return ModelCapability(
        canonical_model=model.canonical_model,
        display_name=model.display_name,
        aliases=aliases,
        available=True,
        hidden=False,
        is_default=False,
        context_length=model.context_length,
        max_output_tokens=model.max_output_tokens,
        reasoning=reasoning,
        supported_efforts=effort_support.levels,
        default_effort=effort_support.default,
        latency_class=model.latency_class,
        input_modalities=("text", "image"),
        supports_tools=None,
        routes=(route,),
        provenance=_provenance(model_sources, observed_at),
    )


def _provenance(
    fact_sources: Mapping[str, str],
    observed_at: datetime,
) -> dict[str, FactProvenance]:
    source_urls = {source.source_key: source.url for source in ClaudeCollector.sources}
    return {
        fact: FactProvenance(
            source_key=source_key,
            source_url=source_urls[source_key],
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


def _model_for_version(
    models: Sequence[_OverviewModel],
    version: str,
) -> _OverviewModel:
    expected = f"claude {version}".casefold()
    match = next((model for model in models if model.display_name.casefold() == expected), None)
    if match is None:
        raise ValueError(f"alias resolves to unknown model version {version!r}")
    return match


def _model_from_behavior(
    models: Sequence[_OverviewModel],
    behavior: str,
    *,
    family: str,
) -> _OverviewModel:
    named = _models_named_in_text(models, behavior)
    family_named = [model for model in named if family in model.display_name.casefold()]
    if family_named:
        return family_named[0]
    family_models = [model for model in models if family in model.display_name.casefold()]
    if not family_models:
        raise ValueError(f"model alias names unknown family {family!r}")
    return family_models[0]


def _models_named_in_text(
    models: Sequence[_OverviewModel],
    text: str,
) -> tuple[_OverviewModel, ...]:
    normalized = _clean_cell(text).casefold()
    return tuple(model for model in models if model.display_name.casefold() in normalized)


def _required_heading_section(document: str, heading: str) -> str:
    heading_match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$",
        document,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if heading_match is None:
        raise ValueError(f"required section {heading!r} is missing")
    body_start = heading_match.end()
    next_heading = re.search(r"^##\s+", document[body_start:], flags=re.MULTILINE)
    body_end = body_start + next_heading.start() if next_heading else len(document)
    return document[body_start:body_end]


def _find_table(document: str, required_headers: tuple[str, ...]) -> _MarkdownTable:
    for table in _markdown_tables(document):
        normalized = tuple(_normalize_cell(header) for header in table.headers)
        if normalized[: len(required_headers)] == required_headers:
            return table
    raise ValueError(f"required Markdown table {required_headers!r} is missing")


def _markdown_tables(document: str) -> tuple[_MarkdownTable, ...]:
    lines = document.splitlines()
    tables: list[_MarkdownTable] = []
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        block: list[str] = []
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            block.append(lines[index])
            index += 1
        if len(block) < 3:
            continue
        rows = tuple(_split_markdown_row(line) for line in block)
        if not _is_separator_row(rows[1]):
            continue
        headers = rows[0]
        body = rows[2:]
        if any(len(row) != len(headers) for row in body):
            continue
        tables.append(_MarkdownTable(headers=headers, rows=body))
    return tuple(tables)


def _split_markdown_row(line: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in line.strip().strip("|").split("|"))


def _is_separator_row(row: tuple[str, ...]) -> bool:
    return bool(row) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in row)


def _required_feature(
    features: Mapping[str, tuple[str, ...]],
    name: str,
    *,
    prefix: bool = False,
) -> tuple[str, ...]:
    if not prefix:
        row = features.get(name)
    else:
        row = next((values for key, values in features.items() if key.startswith(name)), None)
    if row is None:
        raise ValueError(f"models overview table is missing {name!r}")
    return row


def _parse_token_count(value: str) -> int:
    match = re.search(r"(\d+(?:\.\d+)?)\s*([kKmM]?)\s*tokens", _clean_cell(value))
    if match is None:
        raise ValueError(f"invalid token count {value!r}")
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).casefold()]
    return int(Decimal(match.group(1)) * multiplier)


def _normalize_cell(value: str) -> str:
    return " ".join(_clean_cell(value).casefold().split())


def _clean_cell(value: str) -> str:
    without_html = re.sub(r"<[^>]+>", "", value)
    without_links = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", without_html)
    return without_links.replace("**", "").replace("`", "").strip()
