"""Canonical provider metadata.

This registry is intentionally small and import-light so CLI, HTTP routes,
model discovery, web chat, and spawning can share provider facts without
pulling provider runtimes into status paths.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderMetadata:
    """Static provider metadata surfaced to users and runtime policy checks."""

    provider: str
    binary: str
    display_name: str
    user_directory: str
    supports_web_chat: bool = True
    supports_agent_spawn: bool = True
    live_model_discovery: bool = True
    deprecated: bool = False
    deprecation_message: str | None = None
    unavailable_reason: str | None = None

    def installed(self) -> bool:
        """Return whether this provider's CLI binary is available."""
        return shutil.which(self.binary) is not None

    def api_metadata(self) -> dict[str, object]:
        """Return optional provider metadata for HTTP/frontend surfaces."""
        return {
            "display_name": self.display_name,
            "installed": self.installed(),
            "deprecated": self.deprecated,
            "deprecation_message": self.deprecation_message,
            "supports_web_chat": self.supports_web_chat,
            "supports_agent_spawn": self.supports_agent_spawn,
            "unavailable_reason": self.unavailable_reason,
        }


_PROVIDERS: tuple[ProviderMetadata, ...] = (
    ProviderMetadata("claude", "claude", "Claude Code", ".claude"),
    ProviderMetadata("codex", "codex", "Codex", ".codex"),
    ProviderMetadata(
        "droid",
        "droid",
        "Droid",
        ".factory",
        # Live discovery fetches Factory's machine-readable model docs
        # (docs.factory.ai/models.md); no droid CLI endpoint exposes the catalog.
        live_model_discovery=True,
    ),
    ProviderMetadata("grok", "grok", "Grok", ".grok"),
    ProviderMetadata("qwen", "qwen", "Qwen", ".qwen"),
    ProviderMetadata(
        "agy",
        "agy",
        "AGY",
        # AGY retains the upstream CLI's historical config-directory name.
        ".gemini",
    ),
)


def provider_metadata() -> tuple[ProviderMetadata, ...]:
    """Return providers in canonical display/order surfaces use."""
    return _PROVIDERS
