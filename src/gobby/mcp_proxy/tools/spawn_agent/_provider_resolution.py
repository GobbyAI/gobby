"""Provider/model resolution helpers for spawn_agent."""

from __future__ import annotations

MODEL_PROVIDER_PREFIXES = {
    "anthropic": "claude",
    "claude": "claude",
    "google": "gemini",
    "gemini": "gemini",
    "xai": "grok",
    "x.ai": "grok",
    "grok": "grok",
    "openai": "codex",
    "codex": "codex",
    "qwen": "qwen",
    "droid": "droid",
    "agy": "agy",
}


def provider_prefixed_model(value: str | None) -> tuple[str, str] | None:
    """Return provider/model from values like ``claude/sonnet-4-6``."""
    if not value or "/" not in value:
        return None
    prefix, model = value.split("/", 1)
    provider = MODEL_PROVIDER_PREFIXES.get(prefix.strip().lower())
    model = model.strip()
    if not provider or not model:
        return None
    if provider == "claude" and model.startswith(("opus-", "sonnet-", "haiku-")):
        model = f"claude-{model}"
    return provider, model


def defaulted_provider(value: str | None) -> str:
    """Return the effective provider for user/agent provider values."""
    if value is None or value == "inherit":
        return "claude"
    return value


__all__ = ["defaulted_provider", "provider_prefixed_model"]
