"""Build the daemon ``tool_chat`` service from configured capability bindings.

Peer of :mod:`gobby.ai._text_generation_builder`. The adapter factory map is
keyed by :class:`~gobby.ai.registry.AIAdapterStyle` (never by provider), matching
how :class:`~gobby.ai._tool_chat_service.ToolChatService` dispatches. Provider
construction (Claude provider, local OpenAI client) is confined to this adapter
factory layer.
"""

from __future__ import annotations

from typing import Any

from gobby.agents.local_model import resolve_vllm_served_model
from gobby.ai._managed_tool_chat_lease import build_managed_tool_chat_lease_factory
from gobby.ai._tool_chat_adapters import (
    ClaudeProviderFactory,
    ClaudeToolChatAdapter,
    OpenAIClientFactory,
    OpenAICompatibleToolChatAdapter,
    OpenAIModelResolver,
)
from gobby.ai._tool_chat_contracts import ToolLoopLimits
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
from gobby.config.ai import GenerationEndpointConfig
from gobby.config.app import DaemonConfig
from gobby.llm.claude import ClaudeLLMProvider
from gobby.llm.local_provider_adapters import create_local_provider_adapter
from gobby.storage.managed_credentials import ManagedCredentialManager


def build_daemon_tool_chat_service(
    config: DaemonConfig,
    *,
    registry: AICapabilityRegistry | None = None,
    credential_manager: ManagedCredentialManager | None = None,
) -> ToolChatService:
    """Build the daemon tool_chat service from configured capability bindings."""
    limits = ToolLoopLimits(**config.ai.generation.tool_loop.model_dump())
    return ToolChatService(
        registry or build_daemon_ai_capability_registry(config),
        adapter_factories=_daemon_tool_chat_adapter_factories(config),
        profile_defaults=config.ai.generation.profile_defaults,
        default_limits=limits,
        lease_factory=(
            build_managed_tool_chat_lease_factory(credential_manager)
            if credential_manager is not None
            else None
        ),
    )


def _daemon_tool_chat_adapter_factories(
    config: DaemonConfig,
) -> dict[AIAdapterStyle, ToolChatAdapterFactory]:
    return {
        AIAdapterStyle.LLM_PROVIDER: lambda: ClaudeToolChatAdapter(
            provider_factory=_claude_provider_factory(config)
        ),
        AIAdapterStyle.OPENAI_COMPATIBLE: lambda: OpenAICompatibleToolChatAdapter(
            client_factory=_local_client_factory(config),
            model_resolver=_local_model_resolver(config),
        ),
        AIAdapterStyle.LOCAL: lambda: OpenAICompatibleToolChatAdapter(
            client_factory=_local_client_factory(config),
            model_resolver=_local_model_resolver(config),
        ),
        AIAdapterStyle.DAEMON: lambda: CodexSpawnToolChatAdapter(
            config=config,
        ),
        AIAdapterStyle.CLI: DroidSpawnToolChatAdapter,
        AIAdapterStyle.ACP: lambda: ACPSpawnToolChatAdapter(config),
    }


def _claude_provider_factory(config: DaemonConfig) -> ClaudeProviderFactory:
    def factory(_binding: CapabilityBinding) -> ClaudeLLMProvider:
        return ClaudeLLMProvider(config)

    return factory


def _binding_endpoint_config(
    config: DaemonConfig, binding: CapabilityBinding
) -> GenerationEndpointConfig:
    endpoint_name = binding.metadata.get("endpoint")
    if not endpoint_name:
        raise ValueError("openai_compatible tool_chat binding is missing 'endpoint' metadata")
    return resolve_generation_endpoint(config, str(endpoint_name))


def _local_client_factory(config: DaemonConfig) -> OpenAIClientFactory:
    def factory(binding: CapabilityBinding) -> Any:
        local_cfg = _binding_endpoint_config(config, binding)
        client = getattr(create_local_provider_adapter(local_cfg), "client", None)
        if client is None:
            endpoint_name = binding.metadata.get("endpoint")
            raise RuntimeError(f"Local client for endpoint {endpoint_name!r} is unavailable")
        return client

    return factory


def _local_model_resolver(config: DaemonConfig) -> OpenAIModelResolver:
    """Resolve the wire model for a local tool_chat binding.

    ``None`` means the endpoint's configured default. vllm endpoints resolve
    through :func:`resolve_vllm_served_model`, so ``model: auto`` becomes the
    single served id and the literal sentinel never reaches the wire.
    """

    async def resolve(binding: CapabilityBinding, model: str | None) -> str:
        local_cfg = _binding_endpoint_config(config, binding)
        requested = model or local_cfg.model
        if local_cfg.protocol != "vllm":
            return requested
        if requested != local_cfg.model:
            local_cfg = local_cfg.model_copy(update={"model": requested})
        return await resolve_vllm_served_model(local_cfg)

    return resolve
