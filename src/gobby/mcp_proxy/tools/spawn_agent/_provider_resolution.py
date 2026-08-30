"""Provider/model resolution helpers for spawn_agent."""

from __future__ import annotations

from typing import Protocol, cast

PROVIDER_ALIASES = {
    "anthropic": "claude",
    "xai": "grok",
    "x.ai": "grok",
    "openai": "codex",
}

SPAWN_CAPABLE_PROVIDERS = frozenset({"agy", "claude", "codex", "droid", "grok", "qwen"})


class _SessionLookup(Protocol):
    def get(self, session_id: str) -> object: ...


def concrete_provider(value: str | None) -> str | None:
    """Normalize a concrete provider value, leaving ``inherit`` unresolved."""
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized or normalized == "inherit":
        return None
    return PROVIDER_ALIASES.get(normalized, normalized)


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
    default_provider: str | None,
) -> str:
    """Resolve a provider from explicit, agent, or configured default values."""
    explicit = concrete_provider(explicit_provider)
    if explicit is not None:
        return explicit
    agent = concrete_provider(agent_provider)
    if agent is not None:
        return agent
    default = concrete_provider(default_provider)
    if default in SPAWN_CAPABLE_PROVIDERS:
        return default
    raise ValueError(
        "Unable to resolve a provider for spawn_agent. Set the provider argument, "
        "configure a concrete provider on the agent definition, or configure a "
        "default provider for the spawning session."
    )


__all__ = [
    "SPAWN_CAPABLE_PROVIDERS",
    "concrete_provider",
    "parent_session_provider",
    "resolve_spawn_provider",
    "spawning_session_provider",
]
