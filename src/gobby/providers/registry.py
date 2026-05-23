"""Canonical provider metadata.

This registry is intentionally small and import-light so CLI, HTTP routes,
model discovery, web chat, and spawning can share provider facts without
pulling provider runtimes into status paths.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

GEMINI_DEPRECATION_MESSAGE = (
    "Gemini CLI support is deprecated in Gobby. Existing sessions and hooks still work; "
    "prefer Grok, Qwen, Codex, Claude, or Droid for new launches."
)
AGY_UNAVAILABLE_REASON = (
    "AGY has no documented machine transport for live web chat or agent spawning yet."
)


@dataclass(frozen=True)
class ProviderMetadata:
    """Static provider metadata surfaced to users and runtime policy checks."""

    provider: str
    binary: str
    display_name: str
    supports_web_chat: bool = True
    supports_agent_spawn: bool = True
    live_model_discovery: bool = True
    installed_only: bool = False
    deprecated: bool = False
    deprecation_message: str | None = None
    unavailable_reason: str | None = None

    def installed(self) -> bool:
        """Return whether this provider's CLI binary is available."""
        return shutil.which(self.binary) is not None

    def path(self) -> str | None:
        """Return the resolved binary path if installed."""
        return shutil.which(self.binary)

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
    ProviderMetadata("claude", "claude", "Claude Code"),
    ProviderMetadata("codex", "codex", "Codex"),
    ProviderMetadata("droid", "droid", "Droid"),
    ProviderMetadata(
        "gemini",
        "gemini",
        "Gemini CLI",
        deprecated=True,
        deprecation_message=GEMINI_DEPRECATION_MESSAGE,
    ),
    ProviderMetadata("grok", "grok", "Grok"),
    ProviderMetadata("qwen", "qwen", "Qwen"),
    ProviderMetadata(
        "agy",
        "agy",
        "AGY",
        supports_web_chat=False,
        supports_agent_spawn=False,
        live_model_discovery=False,
        installed_only=True,
        unavailable_reason=AGY_UNAVAILABLE_REASON,
    ),
)
_BY_PROVIDER = {entry.provider: entry for entry in _PROVIDERS}


def provider_metadata() -> tuple[ProviderMetadata, ...]:
    """Return providers in canonical display/order surfaces use."""
    return _PROVIDERS


def provider_ids() -> tuple[str, ...]:
    """Return canonical provider IDs."""
    return tuple(entry.provider for entry in _PROVIDERS)


def get_provider_metadata(provider: str) -> ProviderMetadata:
    """Return metadata for a provider ID."""
    normalized = provider.strip().lower()
    try:
        return _BY_PROVIDER[normalized]
    except KeyError as exc:
        raise ValueError(f"Unknown provider: {provider!r}") from exc


def installed_provider_metadata(provider: str) -> dict[str, object]:
    """Return provider metadata plus current installation/path facts."""
    entry = get_provider_metadata(provider)
    path = entry.path()
    metadata = entry.api_metadata()
    metadata.update({"path": path, "installed": path is not None})
    return metadata


def provider_status_metadata(provider: str) -> dict[str, object]:
    """Return status-only metadata without probing runtime backends."""
    entry = get_provider_metadata(provider)
    return {
        "display_name": entry.display_name,
        "deprecated": entry.deprecated,
        "deprecation_message": entry.deprecation_message,
        "supports_web_chat": entry.supports_web_chat,
        "supports_agent_spawn": entry.supports_agent_spawn,
        "unavailable_reason": entry.unavailable_reason,
    }
