"""Provider and agent binding policy for web-chat sessions."""

from typing import Any

from gobby.config.feature_base import parse_feature_candidate


def _normalize_runtime_chat_mode(mode: str | None) -> str | None:
    if mode == "accept_edits":
        return "normal"
    return mode


def _normalize_web_chat_provider(provider: Any) -> str | None:
    """Normalize provider identifiers persisted on web-chat sessions."""
    if not isinstance(provider, str):
        return None

    normalized = provider.strip().lower()
    if normalized in {"", "inherit"}:
        return None
    if normalized in {"claude", "grok", "qwen", "codex", "droid", "agy"}:
        return normalized
    return None


def _first_configured_chat_binding(
    daemon_config: Any,
) -> tuple[str, str, str | None] | None:
    """Return the first usable provider/model/reasoning chat candidate."""
    chat_config = getattr(daemon_config, "chat", None)
    for candidate in getattr(chat_config, "candidates", ()) or ():
        try:
            candidate_provider, candidate_model = parse_feature_candidate(candidate)
        except ValueError:
            continue
        normalized_provider = _normalize_web_chat_provider(candidate_provider)
        if normalized_provider is None:
            continue
        reasoning_effort = getattr(candidate, "reasoning_effort", None)
        if not isinstance(reasoning_effort, str):
            reasoning_effort = None
        return normalized_provider, candidate_model, reasoning_effort
    return None
