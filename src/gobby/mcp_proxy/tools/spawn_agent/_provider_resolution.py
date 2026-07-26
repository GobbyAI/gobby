"""Provider/model resolution helpers for spawn_agent."""

from __future__ import annotations

from typing import Protocol, cast

MODEL_PROVIDER_PREFIXES = {
    "anthropic": "claude",
    "claude": "claude",
    "xai": "grok",
    "x.ai": "grok",
    "grok": "grok",
    "openai": "codex",
    "codex": "codex",
    "qwen": "qwen",
    "droid": "droid",
    "agy": "agy",
}

SPAWN_CAPABLE_PROVIDERS = frozenset({"claude", "codex", "droid", "grok", "qwen"})


class _SessionLookup(Protocol):
    def get(self, session_id: str) -> object: ...


def provider_prefixed_model(value: str | None) -> tuple[str, str] | None:
    """Return provider/model from values like ``claude/sonnet-4-6``."""
    if not value or "/" not in value:
        return None
    prefix, model = value.split("/", 1)
    provider = MODEL_PROVIDER_PREFIXES.get(prefix.strip().lower())
    model = model.strip()
    if not provider or not model:
        return None
    if provider == "claude" and model.startswith(("opus-", "sonnet-", "haiku-", "fable-")):
        model = f"claude-{model}"
    return provider, model


def concrete_provider(value: str | None) -> str | None:
    """Normalize a concrete provider value, leaving ``inherit`` unresolved."""
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized or normalized == "inherit":
        return None
    return MODEL_PROVIDER_PREFIXES.get(normalized, normalized)


def parent_session_provider(
    session_manager: object | None,
    parent_session_id: str | None,
) -> str | None:
    """Return the concrete source recorded for a parent session, when available."""
    if session_manager is None or not parent_session_id:
        return None
    try:
        parent_session = cast(_SessionLookup, session_manager).get(parent_session_id)
        source = getattr(parent_session, "source", None)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return source if isinstance(source, str) else None


def spawning_session_provider(
    session_manager: object | None,
    *,
    caller_session_id: str | None,
    parent_session_id: str | None,
) -> str | None:
    """Return the source a spawn inherits its provider from.

    ``parent_session_id`` is a reporting and lineage argument that a caller may
    point at any session, so an agent that reports to a coordinator would drag
    the coordinator's provider into every worker it launches. The session
    issuing the spawn is the one the child continues, so it is consulted first;
    the declared parent serves paths with no ambient caller such as HTTP,
    dispatch, and the scheduler.
    """
    for candidate in (caller_session_id, parent_session_id):
        if not candidate:
            continue
        source = parent_session_provider(session_manager, candidate)
        if concrete_provider(source) is not None:
            return source
    return None


def resolve_spawn_provider(
    *,
    explicit_provider: str | None,
    agent_provider: str | None,
    parent_provider: str | None,
) -> str:
    """Resolve provider inheritance for a spawned agent."""
    explicit = concrete_provider(explicit_provider)
    if explicit is not None:
        return explicit
    agent = concrete_provider(agent_provider)
    if agent is not None:
        return agent
    parent = concrete_provider(parent_provider)
    if parent in SPAWN_CAPABLE_PROVIDERS:
        return parent
    return "claude"


__all__ = [
    "SPAWN_CAPABLE_PROVIDERS",
    "concrete_provider",
    "parent_session_provider",
    "provider_prefixed_model",
    "resolve_spawn_provider",
    "spawning_session_provider",
]
