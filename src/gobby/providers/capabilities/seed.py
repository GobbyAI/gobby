"""Bundled cold-start snapshots for remote provider capability sources."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

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
from gobby.providers.capabilities.store import ProviderCapabilityStore

_BUNDLED_SOURCE = "bundled"
_AGY_SOURCES = ((_BUNDLED_SOURCE, None),)
_CLAUDE_SOURCES = (
    (
        "models-overview",
        "https://platform.claude.com/docs/en/about-claude/models/overview.md",
    ),
    ("model-config", "https://code.claude.com/docs/en/model-config.md"),
    ("effort-docs", "https://platform.claude.com/docs/en/build-with-claude/effort.md"),
)
_DROID_SOURCES = (("factory-models", "https://docs.factory.ai/models.md"),)
_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_MODEL_FACTS = (
    "canonical_model",
    "display_name",
    "aliases",
    "available",
    "hidden",
    "is_default",
    "context_length",
    "max_output_tokens",
    "reasoning",
    "supported_efforts",
    "default_effort",
    "latency_class",
    "input_modalities",
    "supports_tools",
)
_ROUTE_FACTS = (
    "speed_mode",
    "selector",
    "available",
    "usage_multiplier",
    "throughput_multiplier",
    "latency_class",
    "activations",
)


@dataclass(frozen=True)
class _SeedModel:
    model_id: str
    display_name: str
    efforts: tuple[str, ...] | None = None
    default_effort: str | None = None
    base_model_id: str | None = None
    speed_multiplier: str | None = None
    aliases: tuple[str, ...] = ()
    context_length: int | None = None


_CLAUDE_MODELS = (
    ("claude-fable-5", "Claude Fable 5", ("fable",)),
    ("claude-opus-5", "Claude Opus 5", ("opus", "opus[1m]", "opusplan")),
    ("claude-sonnet-5", "Claude Sonnet 5", ("sonnet", "sonnet[1m]")),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", ("haiku",)),
)

_DROID_MODELS = (
    _SeedModel(
        "claude-fable-5", "Claude Fable 5", ("off", "low", "medium", "high", "xhigh", "max"), "high"
    ),
    _SeedModel(
        "claude-opus-5", "Claude Opus 5", ("off", "low", "medium", "high", "xhigh", "max"), "high"
    ),
    _SeedModel(
        "claude-opus-5-fast",
        "Claude Opus 5 Fast Mode",
        ("off", "low", "medium", "high", "xhigh", "max"),
        "high",
        "claude-opus-5",
        "4.0",
    ),
    _SeedModel(
        "claude-opus-4-7",
        "Claude Opus 4.7",
        ("off", "low", "medium", "high", "xhigh", "max"),
        "high",
    ),
    _SeedModel(
        "claude-opus-4-6", "Claude Opus 4.6", ("off", "low", "medium", "high", "max"), "high"
    ),
    _SeedModel(
        "claude-opus-4-6-fast",
        "Claude Opus 4.6 Fast Mode",
        ("off", "low", "medium", "high", "max"),
        "high",
    ),
    _SeedModel(
        "claude-opus-4-5-20251101", "Claude Opus 4.5", ("off", "low", "medium", "high"), "off"
    ),
    _SeedModel(
        "claude-sonnet-4-6", "Claude Sonnet 4.6", ("off", "low", "medium", "high", "max"), "high"
    ),
    _SeedModel(
        "claude-sonnet-4-5-20250929", "Claude Sonnet 4.5", ("off", "low", "medium", "high"), "off"
    ),
    _SeedModel(
        "claude-haiku-4-5-20251001", "Claude Haiku 4.5", ("off", "low", "medium", "high"), "off"
    ),
    _SeedModel("gpt-5.5", "GPT-5.5", ("none", "low", "medium", "high", "xhigh"), "medium"),
    _SeedModel(
        "gpt-5.5-fast",
        "GPT-5.5 Fast Mode",
        ("none", "low", "medium", "high", "xhigh"),
        "medium",
        "gpt-5.5",
        "5.0",
    ),
    _SeedModel("gpt-5.4", "GPT-5.4", ("none", "low", "medium", "high", "xhigh"), "medium"),
    _SeedModel(
        "gpt-5.4-fast", "GPT-5.4 Fast Mode", ("none", "low", "medium", "high", "xhigh"), "medium"
    ),
    _SeedModel("gpt-5.4-mini", "GPT-5.4 Mini", ("none", "low", "medium", "high", "xhigh"), "high"),
    _SeedModel(
        "gpt-5.3-codex", "GPT-5.3-Codex", ("none", "low", "medium", "high", "xhigh"), "medium"
    ),
    _SeedModel(
        "gpt-5.3-codex-fast",
        "GPT-5.3-Codex Fast Mode",
        ("none", "low", "medium", "high", "xhigh"),
        "medium",
        "gpt-5.3-codex",
        "1.4",
    ),
    _SeedModel("gpt-5.2", "GPT-5.2", ("off", "low", "medium", "high", "xhigh"), "low"),
    _SeedModel("gpt-5.2-codex", "GPT-5.2-Codex", ("low", "medium", "high", "xhigh"), "medium"),
    _SeedModel(
        "gemini-3.5-flash", "Gemini 3.5 Flash", ("minimal", "low", "medium", "high"), "medium"
    ),
    _SeedModel("gemini-3.1-pro-preview", "Gemini 3.1 Pro", ("low", "medium", "high"), "high"),
    _SeedModel(
        "gemini-3-flash-preview", "Gemini 3 Flash", ("minimal", "low", "medium", "high"), "high"
    ),
    _SeedModel("minimax-m2.7", "Droid Core (MiniMax M2.7)", ("high",), "high"),
    _SeedModel("minimax-m2.5", "Droid Core (MiniMax M2.5)", ("low", "medium", "high"), "high"),
    _SeedModel("kimi-k2.6", "Droid Core (Kimi K2.6)", ("off", "high"), "high"),
    _SeedModel("kimi-k3", "Droid Core (Kimi K3)", ("off", "high"), "high"),
    _SeedModel("kimi-k2.5", "Droid Core (Kimi K2.5)", ("off", "high"), "high"),
    _SeedModel("glm-5.2", "Droid Core (GLM-5.2)", ("off", "high", "max"), "high"),
    _SeedModel("glm-5.2-fast", "Droid Core (GLM-5.2 Fast)", ("off", "high", "max"), "high"),
    _SeedModel("glm-5.1", "Droid Core (GLM-5.1)", ("off", "high"), "high"),
    _SeedModel("glm-5", "Droid Core (GLM-5)"),
    _SeedModel("glm-4.7", "Droid Core (GLM-4.7) [Deprecated]"),
    _SeedModel(
        "gpt-5.1-codex-max",
        "GPT-5.1-Codex-Max [Deprecated]",
        ("low", "medium", "high", "xhigh"),
        "medium",
    ),
)

_AGY_MODELS = (
    _SeedModel(
        "gemini-3.7-flash-high",
        "Gemini 3.7 Flash (High)",
        ("high",),
        "high",
        aliases=("gemini-3.7-flash",),
        context_length=1_048_576,
    ),
    _SeedModel(
        "gemini-3.7-flash-medium",
        "Gemini 3.7 Flash (Medium)",
        ("medium",),
        "medium",
        aliases=("gemini-3.7-flash",),
        context_length=1_048_576,
    ),
    _SeedModel(
        "gemini-3.7-flash-low",
        "Gemini 3.7 Flash (Low)",
        ("low",),
        "low",
        aliases=("gemini-3.7-flash",),
        context_length=1_048_576,
    ),
    _SeedModel(
        "gemini-3.6-flash-high",
        "Gemini 3.6 Flash (High)",
        ("high",),
        "high",
        aliases=("gemini-3.6-flash",),
        context_length=1_048_576,
    ),
    _SeedModel(
        "gemini-3.6-flash-medium",
        "Gemini 3.6 Flash (Medium)",
        ("medium",),
        "medium",
        aliases=("gemini-3.6-flash",),
        context_length=1_048_576,
    ),
    _SeedModel(
        "gemini-3.6-flash-low",
        "Gemini 3.6 Flash (Low)",
        ("low",),
        "low",
        aliases=("gemini-3.6-flash",),
        context_length=1_048_576,
    ),
    _SeedModel(
        "gemini-3.5-flash-high",
        "Gemini 3.5 Flash (High)",
        ("high",),
        "high",
        aliases=("gemini-3.5-flash",),
        context_length=1_048_576,
    ),
    _SeedModel(
        "gemini-3.5-flash-medium",
        "Gemini 3.5 Flash (Medium)",
        ("medium",),
        "medium",
        aliases=("gemini-3.5-flash",),
        context_length=1_048_576,
    ),
    _SeedModel(
        "gemini-3.5-flash-low",
        "Gemini 3.5 Flash (Low)",
        ("low",),
        "low",
        aliases=("gemini-3.5-flash",),
        context_length=1_048_576,
    ),
    _SeedModel(
        "gemini-3.1-pro-high",
        "Gemini 3.1 Pro (High)",
        ("high",),
        "high",
        aliases=("gemini-3.1-pro",),
        context_length=1_000_000,
    ),
    _SeedModel(
        "gemini-3.1-pro-low",
        "Gemini 3.1 Pro (Low)",
        ("low",),
        "low",
        aliases=("gemini-3.1-pro",),
        context_length=1_000_000,
    ),
    _SeedModel(
        "claude-sonnet-4-6",
        "Claude Sonnet 4.6 (Thinking)",
        context_length=200_000,
    ),
    _SeedModel(
        "claude-opus-4-6-thinking",
        "Claude Opus 4.6 (Thinking)",
        aliases=("claude-opus-4-6",),
        context_length=1_000_000,
    ),
    _SeedModel(
        "gpt-oss-120b-medium",
        "GPT-OSS 120B (Medium)",
        ("medium",),
        "medium",
        aliases=("gpt-oss-120b",),
        context_length=131_072,
    ),
)


def apply_seed(store: ProviderCapabilityStore) -> None:
    """Persist cold-start rows for remote providers that have no live rows."""
    observed_at = datetime.now(UTC)
    for snapshot in (
        _agy_snapshot(observed_at),
        _claude_snapshot(observed_at),
        _droid_snapshot(observed_at),
    ):
        if not store.has_rows(snapshot.provider):
            store.replace_provider_snapshot(snapshot)


def _claude_snapshot(observed_at: datetime) -> ProviderSnapshot:
    models = tuple(
        _model(
            _SeedModel(model_id, display_name, _CLAUDE_EFFORTS),
            aliases=aliases,
            observed_at=observed_at,
        )
        for model_id, display_name, aliases in _CLAUDE_MODELS
    )
    return ProviderSnapshot(
        provider="claude",
        generation=0,
        models=models,
        sources=_stale_sources(_CLAUDE_SOURCES),
    )


def _droid_snapshot(observed_at: datetime) -> ProviderSnapshot:
    fast_by_base = {model.base_model_id: model for model in _DROID_MODELS if model.base_model_id}
    models = tuple(
        _model(
            model,
            aliases=(),
            observed_at=observed_at,
            fast_model=fast_by_base.get(model.model_id),
        )
        for model in _DROID_MODELS
        if model.base_model_id is None
    )
    return ProviderSnapshot(
        provider="droid",
        generation=0,
        models=models,
        sources=_stale_sources(_DROID_SOURCES),
    )


def _agy_snapshot(observed_at: datetime) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider="agy",
        generation=0,
        models=tuple(
            _model(model, aliases=model.aliases, observed_at=observed_at) for model in _AGY_MODELS
        ),
        sources=_stale_sources(_AGY_SOURCES),
    )


def _model(
    spec: _SeedModel,
    *,
    aliases: tuple[str, ...],
    observed_at: datetime,
    fast_model: _SeedModel | None = None,
) -> ModelCapability:
    routes = [_route(spec, SpeedMode.STANDARD, observed_at)]
    if fast_model is not None:
        routes.append(_route(fast_model, SpeedMode.FAST, observed_at))
    return ModelCapability(
        canonical_model=spec.model_id,
        display_name=spec.display_name,
        aliases=aliases,
        available=True,
        hidden=False,
        is_default=False,
        context_length=spec.context_length,
        max_output_tokens=None,
        reasoning=(
            ReasoningSupport.KNOWN if spec.efforts is not None else ReasoningSupport.UNSUPPORTED
        ),
        supported_efforts=spec.efforts,
        default_effort=spec.default_effort,
        latency_class=None,
        input_modalities=None,
        supports_tools=None,
        routes=tuple(routes),
        provenance=_provenance(_MODEL_FACTS, observed_at),
    )


def _route(spec: _SeedModel, speed_mode: SpeedMode, observed_at: datetime) -> ModelRoute:
    activations: tuple[ActivationDescriptor, ...] = ()
    if speed_mode is SpeedMode.FAST:
        activations = tuple(
            ActivationDescriptor(kind="model_selector", surface=surface, params={})
            for surface in ("spawn-cli", "tool-chat")
        )
    return ModelRoute(
        speed_mode=speed_mode,
        selector=spec.model_id,
        available=True,
        usage_multiplier=(
            Decimal(spec.speed_multiplier) if spec.speed_multiplier is not None else None
        ),
        throughput_multiplier=None,
        latency_class=None,
        activations=activations,
        provenance=_provenance(_ROUTE_FACTS, observed_at),
    )


def _stale_sources(
    sources: tuple[tuple[str, str | None], ...],
) -> tuple[SourceHealth, ...]:
    return tuple(
        SourceHealth(
            source_key=source_key,
            source_url=source_url,
            required=True,
            state=SourceState.STALE,
            attempts=0,
            last_attempt_at=None,
            last_success_at=None,
            last_error=None,
        )
        for source_key, source_url in sources
    )


def _provenance(fields: tuple[str, ...], observed_at: datetime) -> dict[str, FactProvenance]:
    provenance = FactProvenance(
        source_key=_BUNDLED_SOURCE,
        source_url=None,
        observed_at=observed_at,
    )
    return dict.fromkeys(fields, provenance)
