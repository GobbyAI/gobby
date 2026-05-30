"""Daemon-owned AI capability registry."""

from gobby.ai.registry import (
    CANONICAL_AI_CAPABILITIES,
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    CapabilityStatus,
    CapabilityUnavailableError,
    build_daemon_ai_capability_registry,
    normalize_capability,
)
from gobby.ai.text_generation import (
    ACPTextGenerateAdapter,
    DroidCLITextGenerateAdapter,
    LLMProviderTextGenerateAdapter,
    TextGenerateAdapter,
    TextGenerationRequest,
    TextGenerationService,
    build_daemon_text_generation_service,
)

__all__ = [
    "ACPTextGenerateAdapter",
    "AIAdapterStyle",
    "AICapability",
    "AICapabilityRegistry",
    "CANONICAL_AI_CAPABILITIES",
    "CapabilityBinding",
    "CapabilityStatus",
    "CapabilityUnavailableError",
    "DroidCLITextGenerateAdapter",
    "LLMProviderTextGenerateAdapter",
    "TextGenerateAdapter",
    "TextGenerationRequest",
    "TextGenerationService",
    "build_daemon_ai_capability_registry",
    "build_daemon_text_generation_service",
    "normalize_capability",
]
