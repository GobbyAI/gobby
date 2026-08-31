"""Daemon AI capability binding construction helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Any

from gobby.agents.local_model import VLLM_TOOL_CALLING_HINT
from gobby.ai.embeddings import EmbeddingService
from gobby.ai.endpoints import endpoint_provider

# This builder is an internal split of gobby.ai.registry; reuse registry-private
# normalization so binding comparisons keep the same semantics without a public API.
from gobby.ai.registry import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    _normalize_binding_model,
    _normalize_provider,
)
from gobby.config.ai import GenerationEndpointConfig
from gobby.config.feature_base import (
    candidate_labels,
    iter_feature_default_configs,
    parse_feature_candidate,
)
from gobby.llm.local_provider_adapters import OPENAI_CLIENT_PROTOCOLS
from gobby.providers import ProviderMetadata, provider_metadata
from gobby.servers.provider_model_defaults import AGY_MODELS

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig


def build_daemon_ai_capability_registry(
    config: DaemonConfig | None = None,
    *,
    provider_installed: Callable[[ProviderMetadata], bool] | None = None,
    whisper_runtime_available: Callable[[DaemonConfig], bool],
    provider_install_probe_ttl_seconds: float,
) -> AICapabilityRegistry:
    """Build the daemon's baseline AI capability registry."""
    installed = provider_installed or (lambda entry: entry.installed())
    feature_models_by_provider = _feature_candidate_models_by_provider(config)
    bindings: list[CapabilityBinding] = [
        _embedding_binding(config),
    ]
    bindings.extend(_generation_endpoint_text_bindings(config, feature_models_by_provider))
    bindings.extend(_generation_endpoint_tool_bindings(config, feature_models_by_provider))
    bindings.extend(_generation_endpoint_vision_bindings(config, feature_models_by_provider))
    bindings.extend(_audio_bindings(config, whisper_runtime_available))

    for entry in provider_metadata():
        text_binding = _text_generate_binding(
            entry,
            installed,
            feature_models_by_provider,
            provider_install_probe_ttl_seconds,
        )
        if text_binding is not None:
            bindings.append(text_binding)
        tool_chat_binding = _tool_chat_binding(
            entry,
            installed,
            feature_models_by_provider,
            provider_install_probe_ttl_seconds,
        )
        if tool_chat_binding is not None:
            bindings.append(tool_chat_binding)
        if entry.provider == "agy":
            continue
        vision_binding = _vision_extract_binding(
            entry,
            installed,
            feature_models_by_provider,
            provider_install_probe_ttl_seconds,
        )
        if vision_binding is not None:
            bindings.append(vision_binding)
        bindings.append(
            _provider_binding(
                AICapability.AGENT_SPAWN,
                entry,
                installed,
                feature_models_by_provider,
                provider_install_probe_ttl_seconds,
            )
        )
        bindings.append(
            _provider_binding(
                AICapability.WEB_CHAT,
                entry,
                installed,
                feature_models_by_provider,
                provider_install_probe_ttl_seconds,
            )
        )

    bindings.extend(_agy_unavailable_bindings())
    return AICapabilityRegistry(bindings)


def _feature_candidate_models_by_provider(
    config: DaemonConfig | None,
) -> dict[str, tuple[str, ...]]:
    if config is None:
        return {}
    models_by_provider: dict[str, list[str]] = {}
    for feature_config in iter_feature_default_configs(config):
        for candidate in candidate_labels(feature_config.candidates):
            try:
                provider, model = parse_feature_candidate(candidate)
            except ValueError:
                continue
            normalized_provider = _normalize_provider(provider)
            normalized_model = _normalize_binding_model(normalized_provider, model)
            provider_models = models_by_provider.setdefault(normalized_provider, [])
            if normalized_model not in provider_models:
                provider_models.append(normalized_model)
    return {provider: tuple(models) for provider, models in models_by_provider.items()}


def _embedding_binding(config: DaemonConfig | None) -> CapabilityBinding:
    if config is None:
        return CapabilityBinding.unavailable(
            AICapability.EMBED,
            "local",
            adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
            reason="No daemon embedding config supplied.",
        )

    embeddings = config.embeddings
    configured = EmbeddingService.from_config(embeddings).is_configured()
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
                "Embedding capability requires an API base for local/OpenAI-compatible "
                "models or an API key for OpenAI cloud models."
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


def _whisper_transcribe_binding(
    config: DaemonConfig | None,
    whisper_runtime_available: Callable[[DaemonConfig], bool],
) -> CapabilityBinding:
    return _whisper_audio_binding(
        config,
        AICapability.AUDIO_TRANSCRIBE,
        whisper_runtime_available,
    )


def _whisper_translate_binding(
    config: DaemonConfig | None,
    whisper_runtime_available: Callable[[DaemonConfig], bool],
) -> CapabilityBinding:
    return _whisper_audio_binding(
        config,
        AICapability.AUDIO_TRANSLATE,
        whisper_runtime_available,
    )


def _whisper_audio_binding(
    config: DaemonConfig | None,
    capability: AICapability,
    whisper_runtime_available: Callable[[DaemonConfig], bool],
) -> CapabilityBinding:
    if config is None:
        return CapabilityBinding.unavailable(
            capability,
            "whisper",
            adapter_style=AIAdapterStyle.LOCAL,
            reason="No daemon voice config supplied.",
        )

    voice = config.voice
    metadata: dict[str, object] = {
        "device": voice.whisper_device,
        "compute_type": voice.whisper_compute_type,
    }
    if not voice.enabled:
        reason = "voice.enabled is false."
    elif not voice.stt_enabled:
        reason = "voice.stt_enabled is false."
    elif not whisper_runtime_available(config):
        reason = "faster-whisper is not installed."
        metadata["runtime_available"] = False
    else:
        metadata["runtime_available"] = True
        return CapabilityBinding(
            capability=capability,
            provider="whisper",
            adapter_style=AIAdapterStyle.LOCAL,
            available=True,
            models=(voice.whisper_model_size,),
            metadata=metadata,
        )

    return CapabilityBinding.unavailable(
        capability,
        "whisper",
        adapter_style=AIAdapterStyle.LOCAL,
        reason=reason,
        models=(voice.whisper_model_size,),
        metadata=metadata,
    )


def _audio_bindings(
    config: DaemonConfig | None,
    whisper_runtime_available: Callable[[DaemonConfig], bool],
) -> tuple[CapabilityBinding, ...]:
    bindings: list[CapabilityBinding] = [
        _whisper_transcribe_binding(config, whisper_runtime_available),
        _whisper_translate_binding(config, whisper_runtime_available),
    ]
    if config is None:
        return tuple(bindings)

    seen = {(binding.capability, binding.provider) for binding in bindings}
    for binding_config in config.voice.openai_compatible_audio:
        for binding in _openai_compatible_audio_bindings(binding_config):
            key = (binding.capability, binding.provider)
            if key in seen:
                logging.getLogger("gobby.ai.registry").warning(
                    "Skipping duplicate OpenAI-compatible audio binding for %s provider %r",
                    binding.capability.value,
                    binding.provider,
                )
                continue
            bindings.append(binding)
            seen.add(key)
    return tuple(bindings)


def _openai_compatible_audio_bindings(binding_config: Any) -> tuple[CapabilityBinding, ...]:
    return (
        _openai_compatible_audio_binding(
            binding_config,
            AICapability.AUDIO_TRANSCRIBE,
            enabled=binding_config.transcription_enabled,
        ),
        _openai_compatible_audio_binding(
            binding_config,
            AICapability.AUDIO_TRANSLATE,
            enabled=binding_config.translation_enabled,
        ),
    )


def _openai_compatible_audio_binding(
    binding_config: Any,
    capability: AICapability,
    *,
    enabled: bool,
) -> CapabilityBinding:
    metadata = {
        "url": binding_config.url,
        "timeout_seconds": binding_config.timeout_seconds,
    }
    if enabled:
        return CapabilityBinding(
            capability=capability,
            provider=binding_config.provider,
            adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
            available=True,
            models=(binding_config.model,),
            metadata=metadata,
        )
    return CapabilityBinding.unavailable(
        capability,
        binding_config.provider,
        adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
        reason=f"{capability.value} is disabled for this OpenAI-compatible binding.",
        models=(binding_config.model,),
        metadata=metadata,
    )


def _text_generate_binding(
    entry: ProviderMetadata,
    provider_installed: Callable[[ProviderMetadata], bool],
    feature_models_by_provider: Mapping[str, tuple[str, ...]],
    provider_install_probe_ttl_seconds: float,
) -> CapabilityBinding | None:
    metadata = _metadata_for_generation_binding(entry)
    if entry.provider == "agy":
        models = tuple(AGY_MODELS)
        metadata["model_catalog_source"] = "bundled"
        strict_models = True
    else:
        models = feature_models_by_provider.get(_normalize_provider(entry.provider), ())
        strict_models = False

    adapter_style = _text_generate_adapter_style(entry.provider)
    if adapter_style is None:
        return None

    installed = provider_installed(entry)
    return CapabilityBinding(
        capability=AICapability.TEXT_GENERATE,
        provider=entry.provider,
        adapter_style=adapter_style,
        available=installed,
        reason=f"{entry.display_name} CLI is not installed.",
        models=models,
        strict_models=strict_models,
        metadata=metadata,
        availability_probe=partial(provider_installed, entry),
        availability_probe_ttl_seconds=provider_install_probe_ttl_seconds,
    )


def _generation_endpoint_text_bindings(
    config: DaemonConfig | None,
    feature_models_by_provider: Mapping[str, tuple[str, ...]],
) -> tuple[CapabilityBinding, ...]:
    if config is None:
        return ()
    endpoints = config.ai.generation.endpoints
    bindings: list[CapabilityBinding] = []
    for name, endpoint in endpoints.items():
        provider = endpoint_provider(name)
        models = _generation_endpoint_models(provider, endpoint.model, feature_models_by_provider)
        bindings.append(
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider=provider,
                adapter_style=(
                    AIAdapterStyle.DAEMON
                    if endpoint.wire_api == "responses"
                    else AIAdapterStyle.OPENAI_COMPATIBLE
                ),
                available=True,
                models=models,
                metadata={
                    "display_name": name,
                    "api_base": endpoint.api_base,
                    "endpoint": name,
                    "execution_provider": (
                        "codex" if endpoint.wire_api == "responses" else provider
                    ),
                    "protocol": endpoint.protocol,
                    "wire_api": endpoint.wire_api,
                    "model": endpoint.model,
                    "probed_model": endpoint.probed_model,
                    "input_modalities": (
                        list(endpoint.input_modalities)
                        if endpoint.input_modalities is not None
                        else None
                    ),
                },
            )
        )
    return tuple(bindings)


def _generation_endpoint_models(
    provider: str,
    default_model: str,
    feature_models_by_provider: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    models = list(feature_models_by_provider.get(_normalize_provider(provider), ()))
    normalized_default = _normalize_binding_model(provider, default_model)
    if normalized_default not in models:
        models.append(normalized_default)
    return tuple(models)


def _vision_extract_binding(
    entry: ProviderMetadata,
    provider_installed: Callable[[ProviderMetadata], bool],
    feature_models_by_provider: Mapping[str, tuple[str, ...]],
    provider_install_probe_ttl_seconds: float,
) -> CapabilityBinding | None:
    metadata = _metadata_for_generation_binding(entry)
    models = feature_models_by_provider.get(_normalize_provider(entry.provider), ())

    adapter_style = _vision_extract_adapter_style(entry.provider)
    if adapter_style is None:
        return None

    if entry.provider != "claude":
        return CapabilityBinding.unavailable(
            AICapability.VISION_EXTRACT,
            entry.provider,
            adapter_style=adapter_style,
            reason=(
                "No daemon vision_extract adapter has proven image payload support for "
                f"{entry.display_name}."
            ),
            models=models,
            metadata=metadata,
        )

    installed = provider_installed(entry)
    return CapabilityBinding(
        capability=AICapability.VISION_EXTRACT,
        provider=entry.provider,
        adapter_style=adapter_style,
        available=installed,
        reason=f"{entry.display_name} CLI is not installed.",
        models=models,
        metadata=metadata,
        availability_probe=partial(provider_installed, entry),
        availability_probe_ttl_seconds=provider_install_probe_ttl_seconds,
    )


def _generation_endpoint_vision_bindings(
    config: DaemonConfig | None,
    feature_models_by_provider: Mapping[str, tuple[str, ...]],
) -> tuple[CapabilityBinding, ...]:
    if config is None:
        return ()
    endpoints = config.ai.generation.endpoints
    bindings: list[CapabilityBinding] = []
    for name, endpoint in endpoints.items():
        if endpoint.input_modalities is None or "image" not in endpoint.input_modalities:
            continue
        provider = endpoint_provider(name)
        bindings.append(
            CapabilityBinding(
                capability=AICapability.VISION_EXTRACT,
                provider=provider,
                adapter_style=(
                    AIAdapterStyle.DAEMON
                    if endpoint.wire_api == "responses"
                    else AIAdapterStyle.OPENAI_COMPATIBLE
                ),
                available=True,
                models=_generation_endpoint_models(
                    provider,
                    endpoint.model,
                    feature_models_by_provider,
                ),
                metadata={
                    "display_name": name,
                    "api_base": endpoint.api_base,
                    "endpoint": name,
                    "execution_provider": (
                        "codex" if endpoint.wire_api == "responses" else provider
                    ),
                    "protocol": endpoint.protocol,
                    "wire_api": endpoint.wire_api,
                    "model": endpoint.model,
                    "probed_model": endpoint.probed_model,
                    "input_modalities": list(endpoint.input_modalities),
                },
            )
        )
    return tuple(bindings)


def _metadata_for_generation_binding(entry: ProviderMetadata) -> dict[str, object]:
    return {
        "display_name": entry.display_name,
        "deprecated": entry.deprecated,
        "deprecation_message": entry.deprecation_message,
    }


def _text_generate_adapter_style(provider: str) -> AIAdapterStyle | None:
    if provider == "claude":
        return AIAdapterStyle.LLM_PROVIDER
    if provider == "codex":
        return AIAdapterStyle.DAEMON
    if provider in {"agy", "droid", "grok", "qwen"}:
        return AIAdapterStyle.CLI
    return None


# Adapter styles whose tool_chat adapter is implemented today. A binding for any
# other style is registered as unavailable so it is excluded from tool_chat
# selection yet still visible in /api/llm/status.
# Adapter styles whose tool_chat adapter is implemented today. Dispatch is on
# adapter_style, never provider — codex (DAEMON) is a first-class peer of claude
# (LLM_PROVIDER) and lm-studio (OPENAI_COMPATIBLE), so whichever the configured
# feature profile candidates resolve to (including codex via feature_high) is the
# correct provider-agnostic behavior. A binding for a not-yet-built style is
# registered unavailable so it stays visible in /api/llm/status but is filtered
# from selection.
_TOOL_CHAT_EXECUTABLE_STYLES: frozenset[AIAdapterStyle] = frozenset(
    {
        AIAdapterStyle.LLM_PROVIDER,
        AIAdapterStyle.OPENAI_COMPATIBLE,
        AIAdapterStyle.LOCAL,
        AIAdapterStyle.DAEMON,
        AIAdapterStyle.CLI,
        AIAdapterStyle.ACP,
    }
)


def _tool_chat_adapter_style(provider: str) -> AIAdapterStyle | None:
    # tool_chat is an agentic capability, so it mirrors vision_extract's
    # transport classification, NOT text_generate's. grok/qwen drive their own
    # loop over ACP; droid via its CLI; codex via the app-server daemon; claude
    # via the Agent SDK. AGY can spawn agents, but its global-only MCP config
    # cannot confine a tool set to one request, so it gets no tool_chat binding.
    if provider == "claude":
        return AIAdapterStyle.LLM_PROVIDER
    if provider == "codex":
        return AIAdapterStyle.DAEMON
    if provider in {"grok", "qwen"}:
        return AIAdapterStyle.ACP
    if provider == "droid":
        return AIAdapterStyle.CLI
    return None


def _tool_chat_binding(
    entry: ProviderMetadata,
    provider_installed: Callable[[ProviderMetadata], bool],
    feature_models_by_provider: Mapping[str, tuple[str, ...]],
    provider_install_probe_ttl_seconds: float,
) -> CapabilityBinding | None:
    adapter_style = _tool_chat_adapter_style(entry.provider)
    if adapter_style is None:
        return None

    metadata = _metadata_for_generation_binding(entry)
    models = feature_models_by_provider.get(_normalize_provider(entry.provider), ())
    strict_models = False

    if adapter_style not in _TOOL_CHAT_EXECUTABLE_STYLES:
        metadata["supports_tools"] = False
        return CapabilityBinding.unavailable(
            AICapability.TOOL_CHAT,
            entry.provider,
            adapter_style=adapter_style,
            reason=f"tool_chat is not yet available for adapter style '{adapter_style.value}'.",
            models=models,
            metadata=metadata,
        )

    metadata["supports_tools"] = True
    installed = provider_installed(entry)
    return CapabilityBinding(
        capability=AICapability.TOOL_CHAT,
        provider=entry.provider,
        adapter_style=adapter_style,
        available=installed,
        reason=f"{entry.display_name} CLI is not installed.",
        models=models,
        strict_models=strict_models,
        metadata=metadata,
        availability_probe=partial(provider_installed, entry),
        availability_probe_ttl_seconds=provider_install_probe_ttl_seconds,
    )


def _generation_endpoint_tool_bindings(
    config: DaemonConfig | None,
    feature_models_by_provider: Mapping[str, tuple[str, ...]],
) -> tuple[CapabilityBinding, ...]:
    if config is None:
        return ()
    endpoints = config.ai.generation.endpoints
    bindings: list[CapabilityBinding] = []
    for name, endpoint in endpoints.items():
        if not endpoint.tool_chat:
            continue
        provider = endpoint_provider(name)
        models = _generation_endpoint_models(provider, endpoint.model, feature_models_by_provider)
        adapter_style = (
            AIAdapterStyle.DAEMON
            if endpoint.wire_api == "responses"
            else AIAdapterStyle.OPENAI_COMPATIBLE
        )
        metadata: dict[str, object] = {
            "display_name": name,
            "api_base": endpoint.api_base,
            "endpoint": name,
            "execution_provider": ("codex" if endpoint.wire_api == "responses" else provider),
            "protocol": endpoint.protocol,
            "wire_api": endpoint.wire_api,
            "supports_tools": True,
        }
        unavailable_reason = _tool_chat_endpoint_unavailable_reason(name, endpoint)
        if unavailable_reason is not None:
            metadata["supports_tools"] = False
            bindings.append(
                CapabilityBinding.unavailable(
                    AICapability.TOOL_CHAT,
                    provider,
                    adapter_style=adapter_style,
                    reason=unavailable_reason,
                    models=models,
                    metadata=metadata,
                )
            )
            continue
        bindings.append(
            CapabilityBinding(
                capability=AICapability.TOOL_CHAT,
                provider=provider,
                adapter_style=adapter_style,
                available=True,
                models=models,
                metadata=metadata,
            )
        )
    return tuple(bindings)


def _tool_chat_endpoint_unavailable_reason(
    name: str, endpoint: GenerationEndpointConfig
) -> str | None:
    # probed_tools=False is activation evidence; config.tool_chat is never mutated.
    if endpoint.probed_tools is False:
        reason = (
            f"Tool-call probe failed for endpoint {name!r}; "
            "tool_chat remains configured but unavailable until a successful activation."
        )
        if endpoint.protocol == "vllm":
            reason = f"{reason} Fix: {VLLM_TOOL_CALLING_HINT}."
        return reason
    if endpoint.wire_api == "responses":
        return None
    if endpoint.protocol not in OPENAI_CLIENT_PROTOCOLS:
        return (
            f"Local client for endpoint {name!r} is unavailable: "
            f"{endpoint.protocol} endpoints use a native API without an OpenAI tool-calling client"
        )
    return None


def _vision_extract_adapter_style(provider: str) -> AIAdapterStyle | None:
    if provider == "claude":
        return AIAdapterStyle.LLM_PROVIDER
    if provider == "codex":
        return AIAdapterStyle.DAEMON
    if provider in {"grok", "qwen"}:
        return AIAdapterStyle.ACP
    if provider == "droid":
        return AIAdapterStyle.CLI
    return None


def _provider_binding(
    capability: AICapability,
    entry: ProviderMetadata,
    provider_installed: Callable[[ProviderMetadata], bool],
    feature_models_by_provider: Mapping[str, tuple[str, ...]],
    provider_install_probe_ttl_seconds: float,
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
    models = feature_models_by_provider.get(_normalize_provider(entry.provider), ())
    if not supported:
        return CapabilityBinding.unavailable(
            capability,
            entry.provider,
            adapter_style=adapter_style,
            reason=entry.unavailable_reason
            or f"{entry.display_name} does not support {capability.value}.",
            models=models,
            metadata=metadata,
        )

    installed = provider_installed(entry)
    return CapabilityBinding(
        capability=capability,
        provider=entry.provider,
        adapter_style=adapter_style,
        available=installed,
        reason=f"{entry.display_name} CLI is not installed.",
        models=models,
        metadata=metadata,
        availability_probe=partial(provider_installed, entry),
        availability_probe_ttl_seconds=provider_install_probe_ttl_seconds,
    )


def _adapter_style_for_provider(capability: AICapability, provider: str) -> AIAdapterStyle:
    if capability == AICapability.WEB_CHAT and provider in {"grok", "qwen"}:
        return AIAdapterStyle.ACP
    if capability == AICapability.WEB_CHAT and provider == "codex":
        return AIAdapterStyle.DAEMON
    return AIAdapterStyle.CLI


def _agy_unavailable_bindings() -> tuple[CapabilityBinding, ...]:
    from gobby.providers.version_gate import peek_agy_support

    record = peek_agy_support()
    metadata = {
        "agy_installed_version": record.installed_version,
        "agy_required_version": record.required_version,
        "agy_supported": record.supported,
    }
    available_bindings = tuple(
        CapabilityBinding(
            capability=capability,
            provider="agy",
            adapter_style=_adapter_style_for_provider(capability, "agy"),
            available=record.supported,
            reason=record.reason,
            metadata=metadata,
            availability_probe=lambda: peek_agy_support().supported,
            unavailable_reason_probe=lambda: peek_agy_support().reason,
            availability_probe_ttl_seconds=0.0,
        )
        for capability in (AICapability.AGENT_SPAWN, AICapability.WEB_CHAT)
    )
    unavailable_bindings = tuple(
        CapabilityBinding.unavailable(
            capability,
            "agy",
            reason=reason,
            metadata=metadata,
        )
        for capability, reason in (
            (
                AICapability.TOOL_CHAT,
                "AGY configures MCP servers only globally; a per-request controlled-tool set "
                "cannot be confined to one process",
            ),
            (
                AICapability.VISION_EXTRACT,
                "AGY accepts no image input; vision requires the model to open a file path itself",
            ),
        )
    )
    return (*available_bindings, *unavailable_bindings)
