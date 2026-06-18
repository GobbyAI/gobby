"""Codex OSS local-provider helpers."""

from __future__ import annotations

from typing import Any

CODEX_OSS_LOCAL_PROVIDERS = frozenset({"lmstudio", "ollama"})


def codex_oss_provider_for_local_endpoint(endpoint: Any) -> str:
    """Return Codex OSS local provider name for a local generation endpoint."""
    provider = str(getattr(endpoint, "provider", "") or "").strip().lower()
    if provider not in CODEX_OSS_LOCAL_PROVIDERS:
        raise ValueError(
            "Codex OSS local routing supports provider=lmstudio or provider=ollama; "
            f"got provider={provider or 'unknown'}"
        )
    return provider


def codex_oss_config_overrides(oss_provider: str) -> list[str]:
    """Return Codex app-server config overrides for an OSS local provider."""
    provider = oss_provider.strip().lower()
    if provider not in CODEX_OSS_LOCAL_PROVIDERS:
        raise ValueError(f"Unsupported Codex OSS local provider: {oss_provider}")
    return ['model_provider="oss"', f'oss_provider="{provider}"']
