"""Provider capability matrix for spawned terminal agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReasoningFlagStyle = Literal["claude-effort", "codex-config", "reasoning-effort"]


@dataclass(frozen=True)
class ProviderCapabilities:
    """Terminal-agent capabilities that must stay consistent across spawn paths."""

    reasoning_flag: ReasoningFlagStyle | None = None
    sandbox: bool = False
    sensitive_path_enforcement: bool = False


PROVIDER_CAPABILITIES: dict[str, ProviderCapabilities] = {
    "claude": ProviderCapabilities(
        reasoning_flag="claude-effort",
        sandbox=True,
        sensitive_path_enforcement=True,
    ),
    "codex": ProviderCapabilities(
        reasoning_flag="codex-config",
        sandbox=True,
        sensitive_path_enforcement=False,
    ),
    "droid": ProviderCapabilities(
        reasoning_flag="reasoning-effort",
        sandbox=False,
        sensitive_path_enforcement=False,
    ),
    "grok": ProviderCapabilities(
        reasoning_flag="reasoning-effort",
        sandbox=True,
        sensitive_path_enforcement=False,
    ),
    "qwen": ProviderCapabilities(
        reasoning_flag=None,
        sandbox=True,
        sensitive_path_enforcement=False,
    ),
    "agy": ProviderCapabilities(
        reasoning_flag="claude-effort",
        sandbox=True,
        sensitive_path_enforcement=False,
    ),
}


def provider_capabilities(provider: str) -> ProviderCapabilities:
    """Return terminal-agent capabilities for a provider."""
    return PROVIDER_CAPABILITIES.get(provider, ProviderCapabilities())


def provider_reasoning_flag(provider: str) -> ReasoningFlagStyle | None:
    """Return the CLI flag style used to emit terminal reasoning for a provider."""
    return provider_capabilities(provider).reasoning_flag


def provider_supports_terminal_reasoning(provider: str) -> bool:
    """Return whether spawned terminal reasoning can be applied for a provider."""
    return provider_reasoning_flag(provider) is not None


def provider_supports_sandbox(provider: str) -> bool:
    """Return whether Gobby has a sandbox resolver for a provider."""
    return provider_capabilities(provider).sandbox
