"""Provider capability matrix for spawned terminal agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReasoningFlagStyle = Literal["claude-effort", "codex-config", "reasoning-effort"]


@dataclass(frozen=True)
class ProviderCapabilities:
    """Terminal-agent capabilities that must stay consistent across spawn paths."""

    terminal_reasoning: bool = False
    fallback_reasoning_efforts: frozenset[str] = frozenset()
    reasoning_flag: ReasoningFlagStyle | None = None
    sandbox: bool = False
    sensitive_path_enforcement: bool = False


_STANDARD_REASONING_EFFORTS = frozenset({"low", "medium", "high"})

PROVIDER_CAPABILITIES: dict[str, ProviderCapabilities] = {
    "claude": ProviderCapabilities(
        terminal_reasoning=True,
        fallback_reasoning_efforts=frozenset({"low", "medium", "high", "xhigh", "max"}),
        reasoning_flag="claude-effort",
        sandbox=True,
        sensitive_path_enforcement=True,
    ),
    "codex": ProviderCapabilities(
        terminal_reasoning=True,
        fallback_reasoning_efforts=frozenset({"low", "medium", "high", "xhigh"}),
        reasoning_flag="codex-config",
        sandbox=True,
        sensitive_path_enforcement=False,
    ),
    "droid": ProviderCapabilities(
        terminal_reasoning=True,
        fallback_reasoning_efforts=_STANDARD_REASONING_EFFORTS,
        reasoning_flag="reasoning-effort",
        sandbox=False,
        sensitive_path_enforcement=False,
    ),
    "grok": ProviderCapabilities(
        terminal_reasoning=True,
        fallback_reasoning_efforts=_STANDARD_REASONING_EFFORTS,
        reasoning_flag="reasoning-effort",
        sandbox=True,
        sensitive_path_enforcement=False,
    ),
    "qwen": ProviderCapabilities(
        terminal_reasoning=False,
        fallback_reasoning_efforts=frozenset(),
        reasoning_flag=None,
        sandbox=True,
        sensitive_path_enforcement=False,
    ),
}

KNOWN_REASONING_EFFORTS: frozenset[str] = frozenset(
    effort
    for capabilities in PROVIDER_CAPABILITIES.values()
    for effort in capabilities.fallback_reasoning_efforts
)


def provider_capabilities(provider: str) -> ProviderCapabilities:
    """Return terminal-agent capabilities for a provider."""
    return PROVIDER_CAPABILITIES.get(provider, ProviderCapabilities())


def provider_reasoning_efforts(provider: str) -> frozenset[str]:
    """Return fallback reasoning efforts for provider catalogs without model metadata."""
    return provider_capabilities(provider).fallback_reasoning_efforts


def provider_reasoning_flag(provider: str) -> ReasoningFlagStyle | None:
    """Return the CLI flag style used to emit terminal reasoning for a provider."""
    return provider_capabilities(provider).reasoning_flag


def provider_supports_terminal_reasoning(provider: str) -> bool:
    """Return whether spawned terminal reasoning can be applied for a provider."""
    capabilities = provider_capabilities(provider)
    return capabilities.terminal_reasoning and capabilities.reasoning_flag is not None


def provider_supports_sandbox(provider: str) -> bool:
    """Return whether Gobby has a sandbox resolver for a provider."""
    return provider_capabilities(provider).sandbox
