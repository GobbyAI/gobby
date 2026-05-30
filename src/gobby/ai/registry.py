"""Canonical daemon AI capability registry.

This module defines daemon-owned AI capability vocabulary and reusable status/
selection primitives. Execution adapters and HTTP routes register against this
surface; they do not define their own capability names.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from gobby.providers import AGY_UNAVAILABLE_REASON, ProviderMetadata, provider_metadata
from gobby.search.embeddings import is_embedding_configured

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig


class AICapability(StrEnum):
    """Canonical daemon AI capabilities."""

    EMBED = "embed"
    AUDIO_TRANSCRIBE = "audio_transcribe"
    AUDIO_TRANSLATE = "audio_translate"
    VISION_EXTRACT = "vision_extract"
    TEXT_GENERATE = "text_generate"
    AGENT_SPAWN = "agent_spawn"
    WEB_CHAT = "web_chat"


CANONICAL_AI_CAPABILITIES: tuple[AICapability, ...] = tuple(AICapability)


class AIAdapterStyle(StrEnum):
    """Adapter families that can satisfy a capability binding."""

    ACP = "acp"
    CLI = "cli"
    DAEMON = "daemon"
    LLM_PROVIDER = "llm_provider"
    LOCAL = "local"
    OPENAI_COMPATIBLE = "openai_compatible"
    UNAVAILABLE = "unavailable"


class CapabilityUnavailableError(LookupError):
    """Raised when no available binding can satisfy a capability selection."""

    def __init__(
        self,
        capability: AICapability,
        *,
        provider: str | None = None,
        model: str | None = None,
        reason: str | None = None,
    ) -> None:
        detail = reason or f"No available binding for {capability.value}"
        if provider:
            detail = f"{detail} (provider={provider})"
        if model:
            detail = f"{detail} (model={model})"
        super().__init__(detail)
        self.capability = capability
        self.provider = provider
        self.model = model
        self.reason = reason


def normalize_capability(capability: AICapability | str) -> AICapability:
    """Normalize a public capability input into the canonical enum."""
    if isinstance(capability, AICapability):
        return capability
    value = capability.strip()
    try:
        return AICapability(value)
    except ValueError as exc:
        valid = ", ".join(item.value for item in CANONICAL_AI_CAPABILITIES)
        raise ValueError(f"Unknown AI capability {capability!r}; expected one of: {valid}") from exc


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if not normalized:
        raise ValueError("provider must not be empty")
    return normalized


def _normalize_adapter_style(adapter_style: AIAdapterStyle | str) -> AIAdapterStyle:
    if isinstance(adapter_style, AIAdapterStyle):
        return adapter_style
    try:
        return AIAdapterStyle(adapter_style.strip())
    except ValueError as exc:
        valid = ", ".join(item.value for item in AIAdapterStyle)
        raise ValueError(
            f"Unknown AI adapter style {adapter_style!r}; expected one of: {valid}"
        ) from exc


@dataclass(frozen=True, kw_only=True, init=False)
class CapabilityBinding:
    """One provider binding for one canonical AI capability."""

    capability: AICapability
    provider: str
    adapter_style: AIAdapterStyle
    available: bool
    reason: str | None = None
    models: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        *,
        capability: AICapability | str,
        provider: str,
        adapter_style: AIAdapterStyle | str,
        available: bool,
        reason: str | None = None,
        models: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        normalized_capability = normalize_capability(capability)
        normalized_provider = _normalize_provider(provider)
        normalized_adapter_style = _normalize_adapter_style(adapter_style)
        normalized_models = tuple(model for model in models if model)
        normalized_metadata = MappingProxyType(dict(metadata or {}))
        normalized_reason = reason
        if not available and not normalized_reason:
            normalized_reason = (
                f"{normalized_provider} is unavailable for {normalized_capability.value}"
            )

        object.__setattr__(self, "capability", normalized_capability)
        object.__setattr__(self, "provider", normalized_provider)
        object.__setattr__(self, "adapter_style", normalized_adapter_style)
        object.__setattr__(self, "available", available)
        object.__setattr__(self, "reason", normalized_reason)
        object.__setattr__(self, "models", normalized_models)
        object.__setattr__(self, "metadata", normalized_metadata)

    @classmethod
    def unavailable(
        cls,
        capability: AICapability | str,
        provider: str,
        *,
        adapter_style: AIAdapterStyle | str = AIAdapterStyle.UNAVAILABLE,
        reason: str,
        models: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> CapabilityBinding:
        """Create an unavailable binding with an explicit reason."""
        return cls(
            capability=capability,
            provider=provider,
            adapter_style=adapter_style,
            available=False,
            reason=reason,
            models=models,
            metadata=metadata or {},
        )

    def supports_model(self, model: str | None) -> bool:
        """Return whether this binding can satisfy the requested model."""
        return model is None or not self.models or model in self.models

    def to_dict(self) -> dict[str, Any]:
        """Return API-safe binding status data."""
        return {
            "capability": self.capability.value,
            "provider": self.provider,
            "adapter_style": self.adapter_style.value,
            "available": self.available,
            "reason": self.reason,
            "models": list(self.models),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, kw_only=True)
class CapabilityStatus:
    """Status surface for one canonical capability."""

    capability: AICapability
    bindings: tuple[CapabilityBinding, ...]

    @property
    def available(self) -> bool:
        """Return True when at least one binding is available."""
        return any(binding.available for binding in self.bindings)

    @property
    def reason(self) -> str | None:
        """Return a capability-level unavailable reason when none are available."""
        if self.available:
            return None
        if not self.bindings:
            return f"No capability bindings registered for {self.capability.value}."
        reasons = tuple(binding.reason for binding in self.bindings if binding.reason)
        if not reasons:
            return f"No available bindings for {self.capability.value}."
        if len(reasons) == 1:
            return reasons[0]
        return "; ".join(reasons)

    def to_dict(self) -> dict[str, Any]:
        """Return API-safe capability status data."""
        return {
            "capability": self.capability.value,
            "available": self.available,
            "state": "available" if self.available else "unavailable",
            "reason": self.reason,
            "bindings": [binding.to_dict() for binding in self.bindings],
        }


class AICapabilityRegistry:
    """Registry of daemon-owned AI capability bindings."""

    def __init__(self, bindings: Iterable[CapabilityBinding] = ()) -> None:
        self._bindings: dict[AICapability, list[CapabilityBinding]] = {
            capability: [] for capability in CANONICAL_AI_CAPABILITIES
        }
        for binding in bindings:
            self.register(binding)

    @property
    def capabilities(self) -> tuple[AICapability, ...]:
        """Return canonical capabilities in status display order."""
        return CANONICAL_AI_CAPABILITIES

    def register(self, binding: CapabilityBinding) -> None:
        """Register a provider binding for its capability."""
        existing = self._bindings[normalize_capability(binding.capability)]
        if any(item.provider == binding.provider for item in existing):
            raise ValueError(
                f"Duplicate {binding.capability.value} binding for provider {binding.provider!r}"
            )
        existing.append(binding)

    def status(self, capability: AICapability | str) -> CapabilityStatus:
        """Return status for one canonical capability."""
        normalized = normalize_capability(capability)
        return CapabilityStatus(
            capability=normalized,
            bindings=tuple(self._bindings[normalized]),
        )

    def statuses(self) -> tuple[CapabilityStatus, ...]:
        """Return status for every canonical capability."""
        return tuple(self.status(capability) for capability in CANONICAL_AI_CAPABILITIES)

    def status_snapshot(self) -> dict[str, Any]:
        """Return API-safe registry status data."""
        return {
            "capabilities": {
                status.capability.value: status.to_dict() for status in self.statuses()
            }
        }

    def bindings_for(
        self,
        capability: AICapability | str,
        *,
        provider: str | None = None,
        include_unavailable: bool = True,
    ) -> tuple[CapabilityBinding, ...]:
        """Return bindings for a capability, optionally scoped to one provider."""
        normalized = normalize_capability(capability)
        provider_key = _normalize_provider(provider) if provider else None
        bindings = self._bindings[normalized]
        return tuple(
            binding
            for binding in bindings
            if (include_unavailable or binding.available)
            and (provider_key is None or binding.provider == provider_key)
        )

    def binding(self, capability: AICapability | str, provider: str) -> CapabilityBinding | None:
        """Return one provider binding for a capability, if present."""
        bindings = self.bindings_for(capability, provider=provider)
        return bindings[0] if bindings else None

    def select(
        self,
        capability: AICapability | str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> CapabilityBinding:
        """Select the first available binding that satisfies the request."""
        normalized = normalize_capability(capability)
        candidates = tuple(
            binding
            for binding in self.bindings_for(normalized, provider=provider)
            if binding.supports_model(model)
        )
        for binding in candidates:
            if binding.available:
                return binding

        reason = self._selection_failure_reason(normalized, candidates, provider=provider)
        raise CapabilityUnavailableError(
            normalized,
            provider=provider,
            model=model,
            reason=reason,
        )

    def _selection_failure_reason(
        self,
        capability: AICapability,
        candidates: tuple[CapabilityBinding, ...],
        *,
        provider: str | None,
    ) -> str:
        if provider and not candidates:
            return f"No {capability.value} binding registered for provider {provider!r}."
        if not candidates:
            return self.status(capability).reason or f"No available binding for {capability.value}."
        reasons = tuple(binding.reason for binding in candidates if binding.reason)
        return "; ".join(reasons) if reasons else f"No available binding for {capability.value}."


def build_daemon_ai_capability_registry(
    config: DaemonConfig | None = None,
    *,
    provider_installed: Callable[[ProviderMetadata], bool] | None = None,
) -> AICapabilityRegistry:
    """Build the daemon's baseline AI capability registry."""
    installed = provider_installed or (lambda entry: entry.installed())
    bindings: list[CapabilityBinding] = [
        _embedding_binding(config),
        _whisper_transcribe_binding(config),
        _audio_translate_binding(),
    ]

    for entry in provider_metadata():
        if entry.provider == "agy":
            continue
        bindings.append(_provider_binding(AICapability.AGENT_SPAWN, entry, installed))
        bindings.append(_provider_binding(AICapability.WEB_CHAT, entry, installed))

    bindings.extend(_agy_unavailable_bindings())
    return AICapabilityRegistry(bindings)


def _embedding_binding(config: DaemonConfig | None) -> CapabilityBinding:
    if config is None:
        return CapabilityBinding.unavailable(
            AICapability.EMBED,
            "local",
            adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
            reason="No daemon embedding config supplied.",
        )

    embeddings = config.embeddings
    configured = is_embedding_configured(
        model=embeddings.model,
        api_key=embeddings.api_key,
        api_base=embeddings.api_base,
    )
    metadata = {
        "api_base_configured": bool(embeddings.api_base),
        "dim": embeddings.dim,
    }
    if not configured:
        return CapabilityBinding.unavailable(
            AICapability.EMBED,
            "local",
            adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
            reason=(
                "Embedding capability requires embeddings.api_base for local/"
                "OpenAI-compatible models or embeddings.api_key for OpenAI cloud models."
            ),
            models=(embeddings.model,),
            metadata=metadata,
        )

    provider = "local" if embeddings.api_base else "openai"
    return CapabilityBinding(
        capability=AICapability.EMBED,
        provider=provider,
        adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
        available=True,
        models=(embeddings.model,),
        metadata=metadata,
    )


def _whisper_transcribe_binding(config: DaemonConfig | None) -> CapabilityBinding:
    if config is None:
        return CapabilityBinding.unavailable(
            AICapability.AUDIO_TRANSCRIBE,
            "whisper",
            adapter_style=AIAdapterStyle.LOCAL,
            reason="No daemon voice config supplied.",
        )

    voice = config.voice
    metadata = {
        "device": voice.whisper_device,
        "compute_type": voice.whisper_compute_type,
    }
    if not voice.enabled:
        reason = "voice.enabled is false."
    elif not voice.stt_enabled:
        reason = "voice.stt_enabled is false."
    else:
        return CapabilityBinding(
            capability=AICapability.AUDIO_TRANSCRIBE,
            provider="whisper",
            adapter_style=AIAdapterStyle.LOCAL,
            available=True,
            models=(voice.whisper_model_size,),
            metadata=metadata,
        )

    return CapabilityBinding.unavailable(
        AICapability.AUDIO_TRANSCRIBE,
        "whisper",
        adapter_style=AIAdapterStyle.LOCAL,
        reason=reason,
        models=(voice.whisper_model_size,),
        metadata=metadata,
    )


def _audio_translate_binding() -> CapabilityBinding:
    return CapabilityBinding.unavailable(
        AICapability.AUDIO_TRANSLATE,
        "whisper",
        adapter_style=AIAdapterStyle.LOCAL,
        reason="No audio_translate binding registered.",
    )


def _provider_binding(
    capability: AICapability,
    entry: ProviderMetadata,
    provider_installed: Callable[[ProviderMetadata], bool],
) -> CapabilityBinding:
    supported = (
        entry.supports_agent_spawn
        if capability == AICapability.AGENT_SPAWN
        else entry.supports_web_chat
    )
    adapter_style = _adapter_style_for_provider(capability, entry.provider)
    metadata = {
        "display_name": entry.display_name,
        "deprecated": entry.deprecated,
        "deprecation_message": entry.deprecation_message,
    }
    if not supported:
        return CapabilityBinding.unavailable(
            capability,
            entry.provider,
            adapter_style=adapter_style,
            reason=entry.unavailable_reason
            or f"{entry.display_name} does not support {capability.value}.",
            metadata=metadata,
        )

    if provider_installed(entry):
        return CapabilityBinding(
            capability=capability,
            provider=entry.provider,
            adapter_style=adapter_style,
            available=True,
            metadata=metadata,
        )

    return CapabilityBinding.unavailable(
        capability,
        entry.provider,
        adapter_style=adapter_style,
        reason=f"{entry.display_name} CLI is not installed.",
        metadata=metadata,
    )


def _adapter_style_for_provider(capability: AICapability, provider: str) -> AIAdapterStyle:
    if capability == AICapability.WEB_CHAT and provider in {"gemini", "grok", "qwen"}:
        return AIAdapterStyle.ACP
    if capability == AICapability.WEB_CHAT and provider == "codex":
        return AIAdapterStyle.DAEMON
    return AIAdapterStyle.CLI


def _agy_unavailable_bindings() -> tuple[CapabilityBinding, ...]:
    return tuple(
        CapabilityBinding.unavailable(
            capability,
            "agy",
            reason=AGY_UNAVAILABLE_REASON,
        )
        for capability in (
            AICapability.TEXT_GENERATE,
            AICapability.VISION_EXTRACT,
            AICapability.AGENT_SPAWN,
            AICapability.WEB_CHAT,
        )
    )
