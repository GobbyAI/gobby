"""Source-aware context-window resolution."""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import psycopg

from gobby.llm.context_window_values import positive_context_window
from gobby.providers.capabilities.resolve import (
    CapabilityResolver,
    ContextSource,
)

if TYPE_CHECKING:
    from gobby.config.ai import ModelMetadataAlias
    from gobby.storage.hub.protocol import HubDatabase

ContextLengthSource = Literal[
    "provider_reported",
    "provider_catalog",
    "registry",
    "unknown",
]
ContextWindowSource = Literal[
    "override",
    "provider_reported",
    "provider_catalog",
    "registry",
    "unknown",
]

CONTEXT_LENGTH_SOURCE_KEY = "context_length_source"
CONTEXT_LENGTH_FIELDS = (
    "context_length",
    "contextLength",
    "contextWindow",
    "inputTokenLimit",
    "maxInputTokens",
)
PROVIDER_METADATA_CONTEXT_LENGTH_FIELDS = (
    "model_context_window",
    "modelContextWindow",
)
_QWEN_AUTH_TYPES = frozenset({"qwen-oauth", "openai", "anthropic", "gemini", "vertex-ai"})
_KNOWN_PROVIDER_PREFIXES = (
    "agy/",
    "anthropic/",
    "claude/",
    "codex/",
    "droid/",
    "openai/",
    "google/",
    "grok/",
    "qwen/",
    "z-ai/",
    "moonshotai/",
    "minimax/",
)
# Long-form Claude IDs (e.g. ``claude-opus-4-8``) neither start with the bare
# ``opus``/``sonnet``/``haiku``/``fable`` aliases nor get enumerated per dated version, so
# map them to their family's window by substring. Order is longest-token-first;
# family tokens are mutually exclusive in practice.
_CLAUDE_FAMILY_TOKENS: tuple[tuple[str, str], ...] = (
    ("claude-opus", "opus"),
    ("claude-sonnet", "sonnet"),
    ("claude-haiku", "haiku"),
    ("claude-fable", "fable"),
)
# Trailing long-context markers select the 1M tier of an existing family rather
# than naming a distinct model: ``[1m]``, ``-1m``, ``-context-1m``. Strip them so
# ``claude-opus-4-8[1m]`` normalizes to ``claude-opus-4-8`` and resolves the same.
_CONTEXT_WINDOW_MARKER_RE = re.compile(
    r"(?:\[(?:context[-_]?)?1m\]|[-_](?:context[-_])?1m)$",
    re.IGNORECASE,
)
_ONE_MILLION_CONTEXT_WINDOW = 1_000_000
_VALID_CONTEXT_LENGTH_SOURCES: frozenset[str] = frozenset(
    {"provider_reported", "provider_catalog", "registry", "unknown"}
)
_AUTHORITATIVE_CATALOG_SOURCES: frozenset[str] = frozenset(
    {"provider_reported", "provider_catalog"}
)
logger = logging.getLogger(__name__)
_UNKNOWN_CONTEXT_WINDOW_WARNING_LIMIT = 256
_UNKNOWN_CONTEXT_WINDOW_WARNED_MODELS: set[str] = set()
_UNKNOWN_CONTEXT_WINDOW_WARNING_ORDER: deque[str] = deque()


def reset_unknown_context_window_warnings() -> None:
    """Clear warning-dedup state for isolated tests and service resets."""
    _UNKNOWN_CONTEXT_WINDOW_WARNED_MODELS.clear()
    _UNKNOWN_CONTEXT_WINDOW_WARNING_ORDER.clear()


def _remember_unknown_context_window(warning_key: str) -> bool:
    if warning_key in _UNKNOWN_CONTEXT_WINDOW_WARNED_MODELS:
        return False
    if len(_UNKNOWN_CONTEXT_WINDOW_WARNING_ORDER) >= _UNKNOWN_CONTEXT_WINDOW_WARNING_LIMIT:
        evicted = _UNKNOWN_CONTEXT_WINDOW_WARNING_ORDER.popleft()
        _UNKNOWN_CONTEXT_WINDOW_WARNED_MODELS.remove(evicted)
    _UNKNOWN_CONTEXT_WINDOW_WARNING_ORDER.append(warning_key)
    _UNKNOWN_CONTEXT_WINDOW_WARNED_MODELS.add(warning_key)
    return True


@dataclass(frozen=True)
class ResolvedContextWindow:
    """Context-window value with its winning source."""

    value: int | None
    source: ContextWindowSource


@dataclass(frozen=True)
class ReconciledModelContext:
    """Session model identity paired with its effective observed window."""

    model: str | None
    context_window: int | None


def _apply_context_window_marker_floor(
    resolved: ResolvedContextWindow,
    has_one_million_marker: bool,
) -> ResolvedContextWindow:
    if (
        not has_one_million_marker
        or resolved.value is None
        or resolved.value >= _ONE_MILLION_CONTEXT_WINDOW
    ):
        return resolved
    return ResolvedContextWindow(_ONE_MILLION_CONTEXT_WINDOW, resolved.source)


@dataclass(frozen=True)
class ContextLengthCandidate:
    """Model catalog context length with source metadata."""

    value: int
    source: ContextLengthSource


def coerce_context_length(value: Any) -> int | None:
    """Coerce a JSON-ish value to a positive context-window integer."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 and value.is_integer() else None
    if isinstance(value, str):
        try:
            parsed = int(value.replace("_", ""))
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def valid_context_length_source(value: Any) -> ContextLengthSource | None:
    """Return a known catalog source value."""
    if isinstance(value, str) and value in _VALID_CONTEXT_LENGTH_SOURCES:
        return cast(ContextLengthSource, value)
    return None


def strip_known_provider_prefix(value: str) -> str:
    """Strip provider prefixes used by model registries and wrappers."""
    normalized = value.strip()
    lower = normalized.lower()
    for prefix in _KNOWN_PROVIDER_PREFIXES:
        if lower.startswith(prefix):
            return normalized.split("/", 1)[1]
    return normalized


def strip_qwen_auth_suffix(value: str) -> str:
    """Strip Qwen multi-provider auth suffixes from display model IDs."""
    trimmed = value.strip()
    close_idx = trimmed.rfind(")")
    open_idx = trimmed.rfind("(")
    if open_idx >= 0 and close_idx == len(trimmed) - 1 and open_idx < close_idx:
        model_id = trimmed[:open_idx].strip()
        auth_type = trimmed[open_idx + 1 : close_idx].strip()
        if model_id and auth_type in _QWEN_AUTH_TYPES:
            return model_id
    return trimmed


def strip_context_window_marker_suffix(value: str) -> str:
    """Strip a trailing 1M-context marker (``[1m]``, ``-1m``, ``-context-1m``).

    The marker selects the long-context tier of an existing model family, so it
    must not block family/static matching of the underlying model ID.
    """
    trimmed = value.strip()
    stripped = _CONTEXT_WINDOW_MARKER_RE.sub("", trimmed).strip()
    return stripped or trimmed


def normalize_model_lookup_id(value: str) -> str:
    """Normalize a model ID for catalog/static prefix matching."""
    return strip_context_window_marker_suffix(
        strip_qwen_auth_suffix(strip_known_provider_prefix(value))
    ).lower()


def reconcile_observed_model(
    existing_model: str | None,
    observed_model: str | None,
) -> str | None:
    """Preserve explicit context-tier metadata across equivalent observations."""
    existing = existing_model.strip() if isinstance(existing_model, str) else ""
    observed = observed_model.strip() if isinstance(observed_model, str) else ""
    if not observed:
        return existing or None
    if not existing:
        return observed
    if _CONTEXT_WINDOW_MARKER_RE.search(existing) is not None and normalize_model_lookup_id(
        existing
    ) == normalize_model_lookup_id(observed):
        return existing
    return observed


def context_key_allowed_for_provider(provider: str | None, key: str) -> bool:
    """Avoid letting family aliases leak across unrelated providers."""
    if key in {"opus", "sonnet", "haiku", "fable"} or key.startswith("claude-"):
        return provider in {None, "claude", "droid"}
    if key.startswith("qwen3-coder"):
        return provider in {None, "qwen"}
    return True


def provider_catalog_context_length_for_model(
    provider: str | None,
    model: str | None,
) -> int | None:
    """Return a provider-matrix context length when one is available."""
    if not provider or not model:
        return None
    resolution = _get_capability_resolver().resolve_context(provider, model)
    return resolution.value if resolution.source is ContextSource.PROVIDER_MATRIX else None


def extract_context_length_candidate(
    model: dict[str, Any],
    *,
    source_if_missing: ContextLengthSource | None = None,
) -> ContextLengthCandidate | None:
    """Extract context metadata from a provider model entry."""
    source = valid_context_length_source(model.get(CONTEXT_LENGTH_SOURCE_KEY))

    for key in CONTEXT_LENGTH_FIELDS:
        context_length = coerce_context_length(model.get(key))
        if context_length is not None:
            return ContextLengthCandidate(
                value=context_length,
                source=source or source_if_missing or "provider_reported",
            )

    top_provider = model.get("top_provider")
    if isinstance(top_provider, dict):
        top_source = valid_context_length_source(top_provider.get(CONTEXT_LENGTH_SOURCE_KEY))
        context_length = coerce_context_length(top_provider.get("context_length"))
        if context_length is not None:
            return ContextLengthCandidate(
                value=context_length,
                source=top_source or source or source_if_missing or "provider_reported",
            )
    return None


def resolve_context_window(
    model: str | None,
    provider_metadata: Any = None,
    overrides: dict[str, int] | None = None,
    *,
    provider: str | None = None,
    provider_reported_context_window: Any | None = None,
    db: HubDatabase | None = None,
) -> int | None:
    """Resolve context window using source-aware provider/registry precedence."""
    resolved = resolve_context_window_with_source(
        model,
        provider_metadata,
        overrides,
        provider=provider,
        provider_reported_context_window=provider_reported_context_window,
        db=db,
    )
    return resolved.value if resolved else None


def reconcile_model_context(
    existing_model: str | None,
    observed_model: str | None,
    observed_context_window: Any = None,
    *,
    provider: str | None = None,
) -> ReconciledModelContext:
    """Reconcile a provider observation with authoritative session model metadata."""
    model = reconcile_observed_model(existing_model, observed_model)
    reported_window = coerce_context_length(observed_context_window)
    resolved_window = resolve_context_window(
        model,
        provider=provider,
        provider_reported_context_window=reported_window,
    )
    return ReconciledModelContext(
        model=model,
        context_window=resolved_window if resolved_window is not None else reported_window,
    )


def resolve_context_window_with_source(
    model: str | None,
    provider_metadata: Any = None,
    overrides: dict[str, int] | None = None,
    *,
    provider: str | None = None,
    provider_reported_context_window: Any | None = None,
    db: HubDatabase | None = None,
) -> ResolvedContextWindow | None:
    """Resolve context window and expose the selected source."""
    if model is not None and not isinstance(model, str):
        raise TypeError("model must be a string or None")
    if overrides is not None and not isinstance(overrides, dict):
        raise TypeError("overrides must be a dict or None")
    if not model or not model.strip():
        return None

    has_one_million_marker = _CONTEXT_WINDOW_MARKER_RE.search(model.strip()) is not None
    model_lower = model.lower()
    caller_override: int | None = None
    for substr, window in (overrides or {}).items():
        context_window = coerce_context_length(window)
        if substr and context_window is not None and substr.lower() in model_lower:
            caller_override = context_window
            break

    reported = coerce_context_length(provider_reported_context_window)
    if reported is None and isinstance(provider_metadata, dict):
        reported = _coerce_context_length_from_fields(
            provider_metadata,
            PROVIDER_METADATA_CONTEXT_LENGTH_FIELDS,
        )
    provider_name = provider.strip().lower() if isinstance(provider, str) else None
    resolution = _get_capability_resolver(db).resolve_context(
        provider_name or "",
        model,
        caller_override=caller_override,
        route_override=reported,
    )
    source_map: dict[ContextSource, ContextWindowSource] = {
        ContextSource.CALLER_OVERRIDE: "override",
        ContextSource.ROUTE_OVERRIDE: "provider_reported",
        ContextSource.PROVIDER_MATRIX: "provider_catalog",
        ContextSource.OPENROUTER: "registry",
        ContextSource.UNKNOWN: "unknown",
    }
    if resolution.value is not None:
        return _apply_context_window_marker_floor(
            ResolvedContextWindow(resolution.value, source_map[resolution.source]),
            has_one_million_marker,
        )

    warning_key = normalize_model_lookup_id(model)
    if _remember_unknown_context_window(warning_key):
        logger.warning("Context window is unknown for model %s", model)
    return ResolvedContextWindow(None, "unknown")


def _lookup_context_length(
    values: dict[str, int],
    provider: str | None,
    model: str | None,
) -> int | None:
    if not model:
        return None

    normalized_provider = provider.strip().lower() if isinstance(provider, str) else None
    normalized_model = normalize_model_lookup_id(model)
    exact = values.get(normalized_model)
    if exact is not None and context_key_allowed_for_provider(
        normalized_provider, normalized_model
    ):
        return exact

    best_len = 0
    best_value: int | None = None
    for key, value in values.items():
        if not context_key_allowed_for_provider(normalized_provider, key):
            continue
        if normalized_model.startswith(key) and len(key) > best_len:
            best_len = len(key)
            best_value = value
    if best_value is not None:
        return best_value

    # Additive family fallback: runs only after exact and prefix matching fail,
    # so bare aliases (opus/sonnet/haiku) and enumerated IDs still resolve exactly
    # as before. Long-form IDs like ``claude-opus-4-8`` (and future ``-4-9``) map
    # to their family window by substring rather than a per-version table. No-op
    # for catalogs (e.g. droid) that don't carry the bare family keys.
    for token, family in _CLAUDE_FAMILY_TOKENS:
        if token in normalized_model and context_key_allowed_for_provider(
            normalized_provider, family
        ):
            family_value = values.get(family)
            if family_value is not None:
                return family_value
    return None


def _coerce_context_length_from_fields(
    values: Mapping[str, Any],
    fields: tuple[str, ...],
) -> int | None:
    for field in fields:
        context_length = coerce_context_length(values.get(field))
        if context_length is not None:
            return context_length
    return None


def _registry_context_window(
    provider: str | None,
    model: str,
    db: HubDatabase | None = None,
) -> int | None:
    from gobby.llm.model_registry import lookup_context_window

    for candidate in _registry_lookup_candidates(model):
        registry_val = (
            lookup_context_window(candidate, db=db)
            if db is not None
            else lookup_context_window(candidate)
        )
        context_window = positive_context_window(registry_val)
        if context_window is not None:
            return context_window
    return None


def _registry_lookup_candidates(model: str) -> list[str]:
    """Registry lookups key on the bare model ID alone; provider never keys metadata."""
    stripped = strip_qwen_auth_suffix(model)
    return list(dict.fromkeys(candidate for candidate in (model, stripped) if candidate))


class _UnavailableCapabilityService:
    def get_provider_snapshot(self, provider: str) -> None:
        return None


class _RegistryMetadataStore:
    def __init__(self, db: HubDatabase | None) -> None:
        self._db = db

    def get_context_window(self, model: str) -> int | None:
        return _registry_context_window(None, model, self._db)

    def get_model_metadata(self, model: str) -> None:
        return None


def _get_capability_resolver(db: HubDatabase | None = None) -> CapabilityResolver:
    from gobby.app_context import get_app_context

    ctx = get_app_context()
    resolver = getattr(ctx, "provider_capability_resolver", None) if ctx else None
    if resolver is not None and db is None:
        return cast(CapabilityResolver, resolver)
    service = getattr(ctx, "provider_capability_service", None) if ctx else None
    return CapabilityResolver(
        service if service is not None else _UnavailableCapabilityService(),
        _RegistryMetadataStore(db),
        _model_metadata_aliases(ctx, db),
    )


def _model_metadata_aliases(
    ctx: object | None,
    db: HubDatabase | None,
) -> list[ModelMetadataAlias]:
    from gobby.config.ai import default_model_metadata_aliases, parse_model_metadata_aliases

    if db is not None:
        from gobby.storage.config_repository import ConfigRepository, ConfigRepositoryError

        try:
            value = (
                ConfigRepository(db)
                .read(resolve_secrets=False)
                .values.get("ai.model_metadata_aliases")
            )
            return list(parse_model_metadata_aliases(value))
        except (ConfigRepositoryError, OSError, TypeError, ValueError, psycopg.Error):
            logger.debug("Failed to read model metadata aliases", exc_info=True)
            return default_model_metadata_aliases()

    runtime = getattr(ctx, "config_runtime", None) if ctx else None
    if runtime is not None and runtime.ready:
        return list(runtime.capture().snapshot.active.ai.model_metadata_aliases)
    return default_model_metadata_aliases()


__all__ = [
    "CONTEXT_LENGTH_SOURCE_KEY",
    "ContextLengthCandidate",
    "ContextLengthSource",
    "ContextWindowSource",
    "ResolvedContextWindow",
    "coerce_context_length",
    "context_key_allowed_for_provider",
    "extract_context_length_candidate",
    "normalize_model_lookup_id",
    "provider_catalog_context_length_for_model",
    "resolve_context_window",
    "resolve_context_window_with_source",
    "strip_known_provider_prefix",
    "strip_qwen_auth_suffix",
    "valid_context_length_source",
]
