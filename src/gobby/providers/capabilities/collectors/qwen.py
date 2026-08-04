"""Qwen capabilities collected from local ACP and settings discovery."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gobby.adapters.acp_client import ACPClient
from gobby.adapters.qwen_acp_client import QwenACPClient
from gobby.agents.trust import authorize_model_discovery_trust
from gobby.config.app import deep_merge
from gobby.paths import get_gobby_home
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
from gobby.servers.provider_model_discovery import (
    discover_acp_models,
    discover_qwen_configured_models,
    discover_qwen_models,
    load_qwen_settings,
    normalize_qwen_model_labels,
)

logger = logging.getLogger(__name__)

_SOURCE_KEY = "local-model-discovery"
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
_ROUTE_FACTS = frozenset({"speed_mode", "selector", "available", "activations"})

type RawModel = Mapping[str, object]
type DiscoverModels = Callable[[], Awaitable[Sequence[RawModel]]]
type Clock = Callable[[], datetime]


class QwenSourceError(ValueError):
    """Raised when local Qwen discovery cannot produce a snapshot."""

    def __init__(self, detail: str) -> None:
        self.source_key = _SOURCE_KEY
        super().__init__(f"Qwen source {_SOURCE_KEY!r} failed: {detail}")


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


async def _discover_acp(client_cls: type[ACPClient]) -> list[dict[str, object]]:
    models = await discover_acp_models(
        client_cls=client_cls,
        which=shutil.which,
        model_discovery_cwd=_model_discovery_cwd,
        authorize_trust=authorize_model_discovery_trust,
        cleanup_tree=shutil.rmtree,
        logger=logger,
    )
    return models


async def _discover_qwen_models() -> Sequence[RawModel]:
    def configured_models() -> list[dict[str, object]]:
        settings = load_qwen_settings(deep_merge=deep_merge, logger=logger)
        return discover_qwen_configured_models(settings)

    return await discover_qwen_models(
        client_cls=QwenACPClient,
        acp_discoverer=_discover_acp,
        configured_model_discoverer=configured_models,
        label_normalizer=normalize_qwen_model_labels,
        which=shutil.which,
    )


@dataclass(frozen=True)
class QwenCollector:
    """Build a Qwen capability snapshot from local ACP and settings metadata."""

    discover_models: DiscoverModels = _discover_qwen_models
    clock: Clock = lambda: datetime.now(UTC)

    provider = "qwen"
    sources = (SourceSpec(_SOURCE_KEY, None, required=True),)

    async def collect(self) -> ProviderSnapshot:
        observed_at = self.clock()
        try:
            raw_models = await self.discover_models()
        except QwenSourceError:
            raise
        except Exception as error:
            raise QwenSourceError(str(error)) from error
        if not raw_models:
            raise QwenSourceError("discovery returned no models")

        try:
            models = tuple(
                _build_model(raw_model, observed_at, index)
                for index, raw_model in enumerate(raw_models)
            )
        except QwenSourceError:
            raise
        except (TypeError, ValueError) as error:
            raise QwenSourceError(str(error)) from error

        return ProviderSnapshot(
            provider=self.provider,
            generation=0,
            models=models,
            sources=(_healthy_source(observed_at),),
        )


def _build_model(raw: RawModel, observed_at: datetime, index: int) -> ModelCapability:
    canonical_model = _required_string(raw.get("value"), f"model entry {index} id")
    display_name = _optional_string(raw.get("label"), f"model {canonical_model!r} label")
    context_length = _context_length(raw.get("context_length"), canonical_model)
    reasoning, supported_efforts, default_effort = _reasoning(raw.get("reasoning"), canonical_model)

    model_facts = set(_MODEL_BASE_FACTS)
    if context_length is not None:
        model_facts.add("context_length")
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
        routes=(_standard_route(canonical_model, observed_at),),
        provenance=_provenance(model_facts, observed_at),
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


def _standard_route(selector: str, observed_at: datetime) -> ModelRoute:
    return ModelRoute(
        speed_mode=SpeedMode.STANDARD,
        selector=selector,
        available=True,
        usage_multiplier=None,
        throughput_multiplier=None,
        latency_class=None,
        activations=(),
        provenance=_provenance(_ROUTE_FACTS, observed_at),
    )


def _context_length(value: object, canonical_model: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"model {canonical_model!r} context length must be a positive integer")
    return value


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


def _provenance(
    facts: Sequence[str] | set[str] | frozenset[str],
    observed_at: datetime,
) -> dict[str, FactProvenance]:
    return {
        fact: FactProvenance(source_key=_SOURCE_KEY, source_url=None, observed_at=observed_at)
        for fact in facts
    }


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
