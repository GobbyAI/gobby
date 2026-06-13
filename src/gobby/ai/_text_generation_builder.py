"""Factory construction for daemon text generation services."""

from __future__ import annotations

from gobby.ai import _text_generation_adapters as _adapters
from gobby.ai._text_generation_contracts import TextGenerateAdapter, TextGenerateAdapterFactory
from gobby.ai._text_generation_service import TextGenerationService
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
    )


def _daemon_text_generation_adapter_factories(
    config: DaemonConfig,
) -> dict[str, TextGenerateAdapterFactory]:
    factories: dict[str, TextGenerateAdapterFactory] = {
        "claude": lambda: _adapters._claude_text_generate_adapter(config),
        "codex": lambda: _adapters.CodexCLITextGenerateAdapter(
            timeout_seconds=config.ai.generation.timeout_seconds,
        ),
        "gemini": lambda: _adapters._GeminiCLITextGenerateAdapter(
            timeout_seconds=config.ai.generation.timeout_seconds,
        ),
        "grok": lambda: _adapters._GrokCLITextGenerateAdapter(
            timeout_seconds=config.ai.generation.timeout_seconds,
        ),
        "qwen": lambda: _adapters._QwenCLITextGenerateAdapter(
            timeout_seconds=config.ai.generation.timeout_seconds,
            openai_endpoints=config.ai.generation.local.endpoints,
        ),
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
