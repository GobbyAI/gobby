"""Source-aware context-window resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

ContextLengthSource = Literal[
    "provider_reported",
    "provider_catalog",
    "registry",
    "static_default",
]
ContextWindowSource = Literal[
    "override",
    "provider_reported",
    "provider_catalog",
    "registry",
    "static_default",
]

CONTEXT_LENGTH_SOURCE_KEY = "context_length_source"
CONTEXT_LENGTH_FIELDS = (
    "context_length",
    "contextLength",
    "contextWindow",
    "inputTokenLimit",
    "maxInputTokens",
)
_QWEN_AUTH_TYPES = frozenset({"qwen-oauth", "openai", "anthropic", "gemini", "vertex-ai"})
_KNOWN_PROVIDER_PREFIXES = (
    "anthropic/",
    "openai/",
    "google/",
    "qwen/",
    "z-ai/",
    "moonshotai/",
    "minimax/",
)
_VALID_CONTEXT_LENGTH_SOURCES: frozenset[str] = frozenset(
    {"provider_reported", "provider_catalog", "registry", "static_default"}
)
_AUTHORITATIVE_CATALOG_SOURCES: frozenset[str] = frozenset(
    {"provider_reported", "provider_catalog"}
)

# Generic fallback defaults. These are intentionally last-resort values.
_STATIC_CONTEXT_LENGTHS: dict[str, int] = {
    "opus": 1_000_000,
    "sonnet": 200_000,
    "haiku": 200_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-6-fast": 1_000_000,
    "claude-opus-4-5": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "gpt-5.5": 258_400,
    "gpt-5.4": 258_400,
    "gpt-5.4-fast": 258_400,
    "gpt-5.4-mini": 258_400,
    "gpt-5.3-codex": 258_400,
    "gpt-5.3-codex-fast": 258_400,
    "gpt-5.3-codex-spark": 258_400,
    "gpt-5.2": 258_400,
    "gpt-5.2-codex": 258_400,
    "gpt-5.1-codex-max": 258_400,
    "gemini-3.1-pro-preview": 1_000_000,
    "gemini-3-flash-preview": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "grok-build": 512_000,
    "qwen3-coder": 262_144,
    "qwen3-coder-plus": 262_144,
    "qwen3-coder-flash": 262_144,
}

# Droid publishes its own model catalog and limits can differ from OpenRouter or
# Codex defaults for the same visible IDs.
_DROID_PROVIDER_CATALOG_CONTEXT_LENGTHS: dict[str, int] = {
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-6-fast": 1_000_000,
    "claude-opus-4-5": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "gpt-5.4": 200_000,
    "gpt-5.4-fast": 200_000,
    "gpt-5.4-mini": 200_000,
    "gpt-5.3-codex": 200_000,
    "gpt-5.3-codex-fast": 200_000,
    "gpt-5.3-codex-spark": 200_000,
    "gpt-5.2": 200_000,
    "gpt-5.2-codex": 200_000,
    "gpt-5.1-codex-max": 200_000,
    "gemini-3.1-pro-preview": 1_000_000,
    "gemini-3-flash-preview": 1_000_000,
    "glm-5.1": 128_000,
    "glm-5": 128_000,
    "glm-4.7": 128_000,
}


@dataclass(frozen=True)
class ResolvedContextWindow:
    """Context-window value with its winning source."""

    value: int
    source: ContextWindowSource


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


def normalize_model_lookup_id(value: str) -> str:
    """Normalize a model ID for catalog/static prefix matching."""
    return strip_qwen_auth_suffix(strip_known_provider_prefix(value)).lower()


def context_key_allowed_for_provider(provider: str | None, key: str) -> bool:
    """Avoid letting family aliases leak across unrelated providers."""
    if key in {"opus", "sonnet", "haiku"}:
        return provider in {None, "claude", "droid"}
    if key.startswith("qwen3-coder"):
        return provider in {None, "qwen"}
    return True


def static_context_length_for_model(provider: str | None, model: str | None) -> int | None:
    """Return a last-resort static context length for known shipped models."""
    return _lookup_context_length(_STATIC_CONTEXT_LENGTHS, provider, model)


def provider_catalog_context_length_for_model(
    provider: str | None,
    model: str | None,
) -> int | None:
    """Return provider-owned catalog context lengths that beat registry data."""
    normalized_provider = provider.strip().lower() if isinstance(provider, str) else None
    if normalized_provider != "droid":
        return None
    return _lookup_context_length(_DROID_PROVIDER_CATALOG_CONTEXT_LENGTHS, provider, model)


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
    catalog: Any | None = None,
    provider_reported_context_window: Any | None = None,
) -> int | None:
    """Resolve context window using source-aware provider/registry precedence."""
    resolved = resolve_context_window_with_source(
        model,
        provider_metadata,
        overrides,
        provider=provider,
        catalog=catalog,
        provider_reported_context_window=provider_reported_context_window,
    )
    return resolved.value if resolved else None


def resolve_context_window_with_source(
    model: str | None,
    provider_metadata: Any = None,
    overrides: dict[str, int] | None = None,
    *,
    provider: str | None = None,
    catalog: Any | None = None,
    provider_reported_context_window: Any | None = None,
) -> ResolvedContextWindow | None:
    """Resolve context window and expose the selected source."""
    if not model:
        return None

    model_lower = model.lower()
    for substr, window in (overrides or {}).items():
        context_window = coerce_context_length(window)
        if context_window is not None and substr.lower() in model_lower:
            return ResolvedContextWindow(context_window, "override")

    reported = coerce_context_length(provider_reported_context_window)
    if reported is None and isinstance(provider_metadata, dict):
        reported = coerce_context_length(
            provider_metadata.get("model_context_window")
            or provider_metadata.get("modelContextWindow")
        )
    if reported is not None:
        return ResolvedContextWindow(reported, "provider_reported")

    provider_name = provider.strip().lower() if isinstance(provider, str) else None
    catalog_static: ResolvedContextWindow | None = None
    catalog_result = _resolve_from_catalog(
        catalog or _get_provider_model_catalog(),
        provider_name,
        model,
    )
    if catalog_result and catalog_result.source in _AUTHORITATIVE_CATALOG_SOURCES:
        return catalog_result
    if catalog_result and catalog_result.source == "static_default":
        catalog_static = catalog_result

    provider_catalog_value = provider_catalog_context_length_for_model(provider_name, model)
    if provider_catalog_value is not None:
        return ResolvedContextWindow(provider_catalog_value, "provider_catalog")

    registry_value = _registry_context_window(provider_name, model)
    if registry_value is not None:
        return ResolvedContextWindow(registry_value, "registry")

    if catalog_static is not None:
        return catalog_static

    static_value = static_context_length_for_model(provider_name, model)
    if static_value is not None:
        return ResolvedContextWindow(static_value, "static_default")

    return None


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
    return best_value


def _resolve_from_catalog(
    catalog: Any | None,
    provider: str | None,
    model: str,
) -> ResolvedContextWindow | None:
    if catalog is None:
        return None

    if hasattr(catalog, "get_context_window_with_source"):
        raw = catalog.get_context_window_with_source(provider, model)
        if isinstance(raw, ResolvedContextWindow):
            return raw
        if isinstance(raw, ContextLengthCandidate):
            return ResolvedContextWindow(raw.value, raw.source)
        if isinstance(raw, tuple) and len(raw) >= 2:
            value = coerce_context_length(raw[0])
            source = valid_context_length_source(raw[1])
            if value is not None and source is not None:
                return ResolvedContextWindow(value, source)
        if isinstance(raw, dict):
            value = coerce_context_length(raw.get("value") or raw.get("context_window"))
            source = valid_context_length_source(raw.get("source"))
            if value is not None and source is not None:
                return ResolvedContextWindow(value, source)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return ResolvedContextWindow(raw, "provider_reported")

    if hasattr(catalog, "get_context_window"):
        value = catalog.get_context_window(provider, model)
        context_window = coerce_context_length(value)
        if context_window is not None:
            return ResolvedContextWindow(context_window, "provider_reported")
    return None


def _registry_context_window(provider: str | None, model: str) -> int | None:
    from gobby.llm.model_registry import lookup_context_window

    for candidate in _registry_lookup_candidates(provider, model):
        registry_val = lookup_context_window(candidate)
        if registry_val is not None:
            return registry_val
    return None


def _registry_lookup_candidates(provider: str | None, model: str) -> list[str]:
    candidates = [model]
    if provider == "qwen":
        candidates.append(strip_qwen_auth_suffix(model))
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _get_provider_model_catalog() -> Any | None:
    try:
        from gobby.app_context import get_app_context

        ctx = get_app_context()
    except (ImportError, AttributeError):
        return None
    return getattr(ctx, "provider_model_catalog", None) if ctx else None


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
    "static_context_length_for_model",
    "strip_known_provider_prefix",
    "strip_qwen_auth_suffix",
    "valid_context_length_source",
]
