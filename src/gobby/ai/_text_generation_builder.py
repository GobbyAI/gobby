"""Factory construction for daemon text generation services."""

from __future__ import annotations

from gobby.ai import _text_generation_adapters as _adapters
from gobby.ai._text_generation_contracts import (
    CodexAppServerClientProvider,
    TextGenerateAdapter,
    TextGenerateAdapterFactory,
)
from gobby.ai._text_generation_service import TextGenerationService
from gobby.ai.registry import AICapabilityRegistry, build_daemon_ai_capability_registry
from gobby.config.app import DaemonConfig


def build_daemon_text_generation_service(
    config: DaemonConfig,
    *,
    registry: AICapabilityRegistry | None = None,
    codex_client_provider: CodexAppServerClientProvider | None = None,
) -> TextGenerationService:
    """Build the daemon text_generate service from configured capability bindings."""
    return TextGenerationService(
        registry or build_daemon_ai_capability_registry(config),
        adapter_factories=_daemon_text_generation_adapter_factories(
            config,
            codex_client_provider=codex_client_provider,
        ),
        profile_defaults=config.ai.generation.profile_defaults,
        candidate_timeout_seconds=config.ai.generation.candidate_timeout_seconds,
    )


def _daemon_text_generation_adapter_factories(
    config: DaemonConfig,
    *,
    codex_client_provider: CodexAppServerClientProvider | None = None,
) -> dict[str, TextGenerateAdapterFactory]:
    factories: dict[str, TextGenerateAdapterFactory] = {
        "claude": lambda: _adapters._claude_text_generate_adapter(config),
        "codex": lambda: _adapters.CodexAppServerTextGenerateAdapter(
            shared_client_provider=codex_client_provider,
            timeout_seconds=config.ai.generation.timeout_seconds,
        ),
        "gemini": _adapters._GeminiCLITextGenerateAdapter,
        "grok": _adapters._GrokCLITextGenerateAdapter,
        "qwen": _adapters._QwenCLITextGenerateAdapter,
        "droid": _adapters.DroidCLITextGenerateAdapter,
    }
    for endpoint_name in config.ai.generation.local.endpoints:
        provider = f"local:{endpoint_name}"
        factories[provider] = _local_text_generate_adapter_factory(config, endpoint_name)
    return factories


def _local_text_generate_adapter_factory(
    config: DaemonConfig,
    endpoint_name: str,
) -> TextGenerateAdapterFactory:
    def create_adapter() -> TextGenerateAdapter:
        return _adapters._local_text_generate_adapter(config, endpoint_name)

    return create_adapter
