"""Factory construction for daemon text generation services."""

from __future__ import annotations

from gobby.ai import _text_generation_adapters as _adapters
from gobby.ai._text_generation_contracts import TextGenerateAdapter, TextGenerateAdapterFactory
from gobby.ai._text_generation_service import TextGenerationService
from gobby.ai.codex_endpoint import (
    codex_endpoint_config_overrides,
    codex_endpoint_env,
)
from gobby.ai.endpoints import endpoint_provider
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
    )


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
        )

    return create_adapter


def _local_text_generate_adapter_factory(
    config: DaemonConfig,
    endpoint_name: str,
) -> TextGenerateAdapterFactory:
    def create_adapter() -> TextGenerateAdapter:
        return _adapters._local_text_generate_adapter(config, endpoint_name)

    return create_adapter
