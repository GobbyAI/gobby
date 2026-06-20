"""Codex OSS local-provider helpers."""

from __future__ import annotations

from typing import Any

CODEX_OSS_LOCAL_PROVIDERS = frozenset({"lmstudio", "ollama"})


def codex_oss_supported_provider_clause() -> str:
    """Return the supported provider clause used in Codex OSS error messages."""
    return " or ".join(f"provider={provider}" for provider in sorted(CODEX_OSS_LOCAL_PROVIDERS))


def codex_oss_provider_for_local_endpoint(endpoint: Any) -> str:
    """Return Codex OSS local provider name for a local generation endpoint."""
    provider = str(getattr(endpoint, "provider", "") or "").strip().lower()
    if not provider:
        raise ValueError(
            f"Codex OSS local routing requires {codex_oss_supported_provider_clause()}"
        )
    if provider not in CODEX_OSS_LOCAL_PROVIDERS:
        raise ValueError(
            f"Codex OSS local routing supports {codex_oss_supported_provider_clause()}; "
            f"got provider={provider or 'unknown'}"
        )
    return provider


def codex_oss_config_overrides(oss_provider: str) -> list[str]:
    """Return Codex app-server config overrides for an OSS local provider."""
    provider = oss_provider.strip().lower()
    if provider not in CODEX_OSS_LOCAL_PROVIDERS:
        raise ValueError(f"Unsupported Codex OSS local provider: {oss_provider}")
    return ['model_provider="oss"', f'oss_provider="{provider}"']
