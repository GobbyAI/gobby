"""Reasoning helpers for spawned-agent execution."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.servers.provider_models import ProviderModelCatalog

AUTO_REASONING_EFFORT = "auto"
ReasoningStatus = Literal[
    "not_requested",
    "applied",
    "unsupported_provider",
    "unsupported_model",
]

_TERMINAL_REASONING_PROVIDERS = frozenset({"claude", "codex", "gemini", "grok"})
_FALLBACK_REASONING_EFFORTS: dict[str, frozenset[str]] = {
    "claude": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "codex": frozenset({"low", "medium", "high", "xhigh"}),
    "gemini": frozenset({"low", "medium", "high"}),
    "grok": frozenset({"low", "medium", "high"}),
}
_fallback_catalog: ProviderModelCatalog | None = None
_fallback_catalog_config: DaemonConfig | None = None
_fallback_catalog_lock = threading.Lock()


def normalize_reasoning_effort(value: str | None) -> str | None:
    """Normalize UI/API reasoning input."""
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized or normalized == AUTO_REASONING_EFFORT:
        return None
    return normalized


def _normalize_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


@dataclass(frozen=True)
class SpawnReasoningResolution:
    """Resolved spawned-agent reasoning metadata."""

    requested_effort: str | None
    effective_effort: str | None
    reasoning_required: bool
    status: ReasoningStatus
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_effort": self.requested_effort,
            "effective_effort": self.effective_effort,
            "required": self.reasoning_required,
            "status": self.status,
            "message": self.message,
        }


def _get_provider_models(provider: str, daemon_config: DaemonConfig | None) -> list[dict[str, Any]]:
    from gobby.app_context import get_app_context

    global _fallback_catalog, _fallback_catalog_config

    ctx = get_app_context()
    catalog = getattr(ctx, "provider_model_catalog", None) if ctx else None
    if catalog is None:
        from gobby.servers.provider_models import ProviderModelCatalog

        with _fallback_catalog_lock:
            if _fallback_catalog is None or daemon_config is not _fallback_catalog_config:
                _fallback_catalog = ProviderModelCatalog(daemon_config)
                _fallback_catalog_config = daemon_config
            catalog = _fallback_catalog
    snapshot = catalog.get_provider_snapshot(provider)
    models = snapshot.get("models")
    return list(models) if isinstance(models, list) else []


def _select_model_entries(
    models: list[dict[str, Any]],
    requested_model: str | None,
) -> list[dict[str, Any]]:
    normalized_requested = _normalize_identifier(requested_model)
    if normalized_requested is None:
        return models

    matched = []
    for model in models:
        candidates = {
            _normalize_identifier(str(model.get("value") or "")),
            _normalize_identifier(str(model.get("canonical_id") or "")),
        }
        if normalized_requested in candidates:
            matched.append(model)
    return matched


def _supported_efforts(models: list[dict[str, Any]], provider: str) -> set[str]:
    supported: set[str] = set()
    for model in models:
        reasoning = model.get("reasoning")
        if not isinstance(reasoning, dict):
            continue
        efforts = reasoning.get("supported_efforts")
        if not isinstance(efforts, list):
            continue
        supported.update(
            normalized
            for value in efforts
            if isinstance(value, str)
            if (normalized := normalize_reasoning_effort(value)) is not None
        )
    if supported:
        return supported
    return set(_FALLBACK_REASONING_EFFORTS.get(provider, frozenset()))


def resolve_spawn_reasoning(
    *,
    provider: str,
    model: str | None,
    requested_effort: str | None,
    reasoning_required: bool | None,
    daemon_config: DaemonConfig | None = None,
) -> SpawnReasoningResolution:
    """Resolve a spawn-time reasoning request for a terminal agent."""
    normalized_request = normalize_reasoning_effort(requested_effort)
    if normalized_request is None:
        return SpawnReasoningResolution(
            requested_effort=None,
            effective_effort=None,
            reasoning_required=False,
            status="not_requested",
        )

    required = bool(reasoning_required)
    models = _get_provider_models(provider, daemon_config)
    matched_models = _select_model_entries(models, model)

    if model and models and not matched_models:
        return SpawnReasoningResolution(
            requested_effort=normalized_request,
            effective_effort=None,
            reasoning_required=required,
            status="unsupported_model",
            message=(
                f"Requested reasoning '{normalized_request}' was not applied because "
                f"model '{model}' is not in the {provider} startup catalog."
            ),
        )

    supported_efforts = _supported_efforts(matched_models or models, provider)
    if normalized_request not in supported_efforts:
        model_label = f" model '{model}'" if model else ""
        return SpawnReasoningResolution(
            requested_effort=normalized_request,
            effective_effort=None,
            reasoning_required=required,
            status="unsupported_model",
            message=(
                f"Requested reasoning '{normalized_request}' is not supported for "
                f"{provider}{model_label}."
            ),
        )

    if provider not in _TERMINAL_REASONING_PROVIDERS:
        return SpawnReasoningResolution(
            requested_effort=normalized_request,
            effective_effort=None,
            reasoning_required=required,
            status="unsupported_provider",
            message=(
                f"Requested reasoning '{normalized_request}' was not applied because "
                f"spawned-terminal reasoning is not wired for provider '{provider}'."
            ),
        )

    return SpawnReasoningResolution(
        requested_effort=normalized_request,
        effective_effort=normalized_request,
        reasoning_required=required,
        status="applied",
    )
