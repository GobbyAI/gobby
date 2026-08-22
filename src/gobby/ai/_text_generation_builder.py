"""Factory construction for daemon text generation services."""

from __future__ import annotations

from typing import Any

from gobby.agents.local_model import LocalModelError, resolve_vllm_served_model
from gobby.ai import _text_generation_adapters as _adapters
from gobby.ai._image_routing import LocalModalityResolver, LocalModelModalities
from gobby.ai._text_generation_contracts import TextGenerateAdapter, TextGenerateAdapterFactory
from gobby.ai._text_generation_service import TextGenerationService
from gobby.ai.codex_endpoint import (
    codex_endpoint_config_overrides,
    codex_endpoint_env,
)
from gobby.ai.endpoint_activation import modalities_for_served_model
from gobby.ai.endpoints import endpoint_provider, resolve_generation_endpoint
from gobby.ai.registry import AICapabilityRegistry, build_daemon_ai_capability_registry
from gobby.config.app import DaemonConfig


def build_daemon_text_generation_service(
    config: DaemonConfig,
    *,
    registry: AICapabilityRegistry | None = None,
) -> TextGenerationService:
    """Build the daemon text_generate service from configured capability bindings."""
    return TextGenerationService(
        registry or build_daemon_ai_capability_registry(config),
        adapter_factories=_daemon_text_generation_adapter_factories(config),
        profile_defaults=config.ai.generation.profile_defaults,
        candidate_timeout_seconds=config.ai.generation.candidate_timeout_seconds,
        cli_candidate_timeout_seconds=config.ai.generation.cli_candidate_timeout_seconds,
        spawn_cold_max_concurrency=config.ai.generation.spawn_cold_max_concurrency,
        local_modality_resolver=build_local_modality_resolver(config),
    )


def build_local_modality_resolver(config: DaemonConfig) -> LocalModalityResolver:
    """Return the live image-modality resolver for local endpoint bindings.

    The candidate model is resolved to its served id first (vllm ``model: auto``
    through :func:`resolve_vllm_served_model`, so the literal sentinel never
    reaches routing). Activation probe evidence for that served model is
    authoritative; otherwise the endpoint's live discovery catalog supplies the
    advertised modalities; neither means the candidate is not image-eligible.
    """

    async def resolve(endpoint_name: str, model: str | None) -> LocalModelModalities:
        # gobby.servers.local_provider_models imports gobby.ai; resolve lazily.
        from gobby.servers.local_provider_models import discover_local_endpoint_model_group

        try:
            endpoint = resolve_generation_endpoint(config, endpoint_name)
        except ValueError as exc:
            return LocalModelModalities(model=None, input_modalities=None, error=str(exc))
        requested = model or endpoint.model
        selected = (
            endpoint
            if requested == endpoint.model
            else endpoint.model_copy(update={"model": requested})
        )
        resolved = requested
        if endpoint.protocol == "vllm":
            try:
                resolved = await resolve_vllm_served_model(selected)
            except LocalModelError as exc:
                return LocalModelModalities(model=None, input_modalities=None, error=str(exc))
        probed = modalities_for_served_model(endpoint, resolved)
        if probed is not None:
            return LocalModelModalities(model=resolved, input_modalities=tuple(probed))
        group = await discover_local_endpoint_model_group(endpoint_name, endpoint)
        advertised = _advertised_input_modalities(group.models, resolved)
        if advertised is None and group.error is not None:
            return LocalModelModalities(model=resolved, input_modalities=None, error=group.error)
        return LocalModelModalities(model=resolved, input_modalities=advertised)

    return resolve


def _advertised_input_modalities(
    models: list[dict[str, Any]],
    model_id: str,
) -> tuple[str, ...] | None:
    for entry in models:
        if entry.get("is_default") is True or entry.get("canonical_id") != model_id:
            continue
        modalities = entry.get("input_modalities")
        if isinstance(modalities, list):
            return tuple(str(item) for item in modalities)
        return None
    return None


def _daemon_text_generation_adapter_factories(
    config: DaemonConfig,
) -> dict[str, TextGenerateAdapterFactory]:
    factories: dict[str, TextGenerateAdapterFactory] = {
        "claude": lambda: _adapters._claude_text_generate_adapter(config),
        "codex": lambda: _adapters.CodexCLITextGenerateAdapter(
            timeout_seconds=config.ai.generation.timeout_seconds,
        ),
        "agy": lambda: _adapters.AgyCLITextGenerateAdapter(
            timeout_seconds=config.ai.generation.timeout_seconds,
        ),
        "grok": lambda: _adapters._GrokCLITextGenerateAdapter(
            timeout_seconds=config.ai.generation.timeout_seconds,
        ),
        "qwen": lambda: _adapters._QwenCLITextGenerateAdapter(
            timeout_seconds=config.ai.generation.timeout_seconds,
            openai_endpoints={
                name: endpoint
                for name, endpoint in config.ai.generation.endpoints.items()
                if endpoint.wire_api == "chat-completions"
            },
        ),
        "droid": _adapters.DroidCLITextGenerateAdapter,
    }
    for endpoint_name, endpoint in config.ai.generation.endpoints.items():
        provider = endpoint_provider(endpoint_name)
        if endpoint.wire_api == "responses":
            factories[provider] = _responses_text_generate_adapter_factory(
                config,
                endpoint_name,
            )
        else:
            factories[provider] = _local_text_generate_adapter_factory(
                config,
                endpoint_name,
            )
    return factories


def _responses_text_generate_adapter_factory(
    config: DaemonConfig,
    endpoint_name: str,
) -> TextGenerateAdapterFactory:
    endpoint = config.ai.generation.endpoints[endpoint_name]

    def create_adapter() -> TextGenerateAdapter:
        return _adapters.CodexCLITextGenerateAdapter(
            timeout_seconds=config.ai.generation.timeout_seconds,
            env=codex_endpoint_env(endpoint),
            config_overrides=codex_endpoint_config_overrides(endpoint_name, endpoint),
            ignore_user_config=endpoint.protocol == "vllm",
        )

    return create_adapter


def _local_text_generate_adapter_factory(
    config: DaemonConfig,
    endpoint_name: str,
) -> TextGenerateAdapterFactory:
    def create_adapter() -> TextGenerateAdapter:
        return _adapters._local_text_generate_adapter(config, endpoint_name)

    return create_adapter
