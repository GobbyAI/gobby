"""Provider/model resolution helpers for spawn_agent.

Precedence for the spawn provider when the caller does not supply ``model``:

1. Explicit ``provider`` argument
2. Agent definition provider
3. Spawning-session default, if it is spawn-capable

When the caller supplies ``model``, the ``provider`` argument is required.
Agent-definition and session defaults cannot fill it in: a named model is a
provider-specific identity, and silently binding it to another CLI is the
failure this module exists to prevent. The resolved pair is then checked
against the provider capability matrix (``CapabilityResolver.find_model`` on
collector snapshots, including Codex ``models_cache.json``) before any
terminal or worktree is allocated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from gobby.providers.capabilities.resolve import CapabilityResolver

from ._runtime import _normalize_optional_model

PROVIDER_ALIASES = {
    "anthropic": "claude",
    "xai": "grok",
    "x.ai": "grok",
    "openai": "codex",
}

SPAWN_CAPABLE_PROVIDERS = frozenset({"agy", "claude", "codex", "droid", "grok", "qwen"})

SpawnArgumentCode = Literal["provider_required_for_model", "incompatible_model_provider"]
PROVIDER_REQUIRED_FOR_MODEL: SpawnArgumentCode = "provider_required_for_model"
INCOMPATIBLE_MODEL_PROVIDER: SpawnArgumentCode = "incompatible_model_provider"


class _SessionLookup(Protocol):
    def get(self, session_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class SpawnArgumentError:
    """Structured spawn argument rejection returned before allocation."""

    error_code: SpawnArgumentCode
    error: str
    model: str | None = None
    provider: str | None = None
    compatible_providers: tuple[str, ...] = ()

    def to_response(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": False,
            "error_code": self.error_code,
            "error": self.error,
        }
        if self.model is not None:
            payload["model"] = self.model
        if self.provider is not None:
            payload["provider"] = self.provider
        if self.compatible_providers:
            payload["compatible_providers"] = list(self.compatible_providers)
        return payload


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
    """Resolve a provider from explicit, agent, or configured default values.

    Precedence is explicit argument, then agent definition, then spawn-capable
    session default. Callers that also supply ``model`` must reject missing
    explicit providers with ``missing_provider_for_supplied_model`` first so
    this function never silently binds a named model to a default CLI.
    """
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


def missing_provider_for_supplied_model(
    *,
    explicit_provider: str | None,
    model: str | None,
) -> SpawnArgumentError | None:
    """Reject a supplied model that has no explicit provider argument."""
    supplied_model = _normalize_optional_model(model)
    if supplied_model is None or concrete_provider(explicit_provider) is not None:
        return None
    return SpawnArgumentError(
        error_code=PROVIDER_REQUIRED_FOR_MODEL,
        error=(
            "spawn_agent requires an explicit provider when model is supplied; "
            f"got model={supplied_model!r} with no provider. Pass provider with "
            "model so the pair is not bound to an agent definition or session default."
        ),
        model=supplied_model,
    )


def incompatible_spawn_model_provider(
    *,
    provider: str,
    model: str | None,
    resolver: CapabilityResolver | None = None,
) -> SpawnArgumentError | None:
    """Reject a model the target provider's capability snapshot does not serve.

    Reuses ``CapabilityResolver.find_model`` on collector snapshots (Codex reads
    ``models_cache.json``, the same catalog that emits "Model metadata for
    `grok-4.6` not found"). Unknown models pass through when the target provider
    has no collector-backed catalog loaded, so the bundled cold-start seed never
    rejects a model newer than itself. Generation-endpoint selectors are skipped.
    """
    supplied_model = _normalize_optional_model(model)
    if supplied_model is None or _is_generation_endpoint_model(supplied_model):
        return None
    capability_resolver = resolver if resolver is not None else spawn_capability_resolver()
    if capability_resolver.find_model(provider, supplied_model) is not None:
        return None
    serving = capability_resolver.providers_for_model(
        supplied_model, tuple(sorted(SPAWN_CAPABLE_PROVIDERS))
    )
    serving = tuple(name for name in serving if name != provider)
    if not serving and not capability_resolver.has_authoritative_provider_catalog(provider):
        return None
    if serving:
        hint = f" Providers that serve this model: {', '.join(serving)}."
    else:
        hint = ""
    return SpawnArgumentError(
        error_code=INCOMPATIBLE_MODEL_PROVIDER,
        error=(f"Model {supplied_model!r} is not supported by provider {provider!r}.{hint}"),
        model=supplied_model,
        provider=provider,
        compatible_providers=serving,
    )


def spawn_capability_resolver() -> CapabilityResolver:
    """Return the daemon capability resolver, or an empty fallback."""
    from gobby.agents.reasoning import _get_capability_resolver

    return _get_capability_resolver()


def _is_generation_endpoint_model(model: str) -> bool:
    return model == "local" or model == "endpoint" or model.startswith("endpoint:")


__all__ = [
    "INCOMPATIBLE_MODEL_PROVIDER",
    "PROVIDER_REQUIRED_FOR_MODEL",
    "SPAWN_CAPABLE_PROVIDERS",
    "SpawnArgumentError",
    "concrete_provider",
    "incompatible_spawn_model_provider",
    "missing_provider_for_supplied_model",
    "parent_session_provider",
    "resolve_spawn_provider",
    "spawn_capability_resolver",
    "spawning_session_provider",
]
