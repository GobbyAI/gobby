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

__all__ = [
    "AIAdapterStyle",
    "AICapability",
    "AICapabilityRegistry",
    "CANONICAL_AI_CAPABILITIES",
    "CapabilityBinding",
    "CapabilityStatus",
    "CapabilityUnavailableError",
    "build_daemon_ai_capability_registry",
    "normalize_capability",
]
