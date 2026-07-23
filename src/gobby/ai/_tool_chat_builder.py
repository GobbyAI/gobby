"""Build the daemon ``tool_chat`` service from configured capability bindings.

Peer of :mod:`gobby.ai._text_generation_builder`. The adapter factory map is
keyed by :class:`~gobby.ai.registry.AIAdapterStyle` (never by provider), matching
how :class:`~gobby.ai._tool_chat_service.ToolChatService` dispatches. Provider
construction (Claude provider, local OpenAI client) is confined to this adapter
factory layer.
"""

from __future__ import annotations

from typing import Any

from gobby.ai._tool_chat_adapters import (
    ClaudeProviderFactory,
    ClaudeToolChatAdapter,
    OpenAIClientFactory,
    OpenAICompatibleToolChatAdapter,
)
from gobby.ai._tool_chat_service import ToolChatAdapterFactory, ToolChatService
from gobby.ai._tool_chat_spawn import (
    ACPSpawnToolChatAdapter,
    CodexSpawnToolChatAdapter,
    DroidSpawnToolChatAdapter,
)
from gobby.ai.endpoints import resolve_generation_endpoint
from gobby.ai.registry import (
    AIAdapterStyle,
    AICapabilityRegistry,
    CapabilityBinding,
    build_daemon_ai_capability_registry,
)
from gobby.config.app import DaemonConfig
from gobby.llm.claude import ClaudeLLMProvider
from gobby.llm.local_provider_adapters import create_local_provider_adapter


def build_daemon_tool_chat_service(
    config: DaemonConfig,
    *,
    registry: AICapabilityRegistry | None = None,
) -> ToolChatService:
    """Build the daemon tool_chat service from configured capability bindings."""
    return ToolChatService(
        registry or build_daemon_ai_capability_registry(config),
        adapter_factories=_daemon_tool_chat_adapter_factories(config),
        profile_defaults=config.ai.generation.profile_defaults,
        attempt_timeout_seconds=config.ai.generation.timeout_seconds,
    )


def _daemon_tool_chat_adapter_factories(
    config: DaemonConfig,
) -> dict[AIAdapterStyle, ToolChatAdapterFactory]:
    return {
        AIAdapterStyle.LLM_PROVIDER: lambda: ClaudeToolChatAdapter(
            provider_factory=_claude_provider_factory(config)
        ),
        AIAdapterStyle.OPENAI_COMPATIBLE: lambda: OpenAICompatibleToolChatAdapter(
            client_factory=_local_client_factory(config)
        ),
        AIAdapterStyle.LOCAL: lambda: OpenAICompatibleToolChatAdapter(
            client_factory=_local_client_factory(config)
        ),
        AIAdapterStyle.DAEMON: lambda: CodexSpawnToolChatAdapter(
            timeout_seconds=config.ai.generation.timeout_seconds,
            config=config,
        ),
        AIAdapterStyle.CLI: lambda: DroidSpawnToolChatAdapter(
            timeout_seconds=config.ai.generation.timeout_seconds,
        ),
        AIAdapterStyle.ACP: lambda: ACPSpawnToolChatAdapter(config),
    }


def _claude_provider_factory(config: DaemonConfig) -> ClaudeProviderFactory:
    def factory(_binding: CapabilityBinding) -> ClaudeLLMProvider:
        return ClaudeLLMProvider(config)

    return factory


def _local_client_factory(config: DaemonConfig) -> OpenAIClientFactory:
    def factory(binding: CapabilityBinding) -> Any:
        endpoint_name = binding.metadata.get("endpoint")
        if not endpoint_name:
            raise ValueError("openai_compatible tool_chat binding is missing 'endpoint' metadata")
        local_cfg = resolve_generation_endpoint(config, str(endpoint_name))
        client = getattr(create_local_provider_adapter(local_cfg), "client", None)
        if client is None:
            raise RuntimeError(f"Local client for endpoint {endpoint_name!r} is unavailable")
        return client

    return factory
