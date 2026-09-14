"""Provider-neutral spawn routing tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.mcp_proxy.tools.spawn_agent._provider_resolution import (
    INCOMPATIBLE_MODEL_PROVIDER,
    PROVIDER_REQUIRED_FOR_MODEL,
    concrete_provider,
    incompatible_spawn_model_provider,
    missing_provider_for_supplied_model,
    parent_session_provider,
    resolve_spawn_provider,
    spawning_session_provider,
)
from gobby.mcp_proxy.tools.spawn_agent._runtime import _normalize_optional_model
from gobby.providers.capabilities.models import (
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
)
from gobby.providers.capabilities.resolve import CapabilityResolver

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("provider", ["agy", "claude", "codex", "droid", "grok", "qwen"])
def test_inherit_uses_spawn_capable_default_provider(provider: str) -> None:
    resolved: str | None
    try:
        resolved = resolve_spawn_provider(
            explicit_provider="inherit",
            agent_provider="inherit",
            default_provider=provider,
        )
    except ValueError:
        resolved = None
    assert resolved == provider


@pytest.mark.parametrize("provider", [None, "", "inherit", "pipeline", "unknown"])
def test_inherit_fails_for_unsupported_default_provider(provider: str | None) -> None:
    with pytest.raises(ValueError, match="Set the provider argument"):
        resolve_spawn_provider(
            explicit_provider=None,
            agent_provider="inherit",
            default_provider=provider,
        )


@pytest.mark.parametrize(
    ("explicit_provider", "agent_provider", "default_provider"),
    [
        ("agy", "claude", "codex"),
        (None, "agy", "claude"),
        ("inherit", "agy", "claude"),
        ("inherit", "inherit", "agy"),
    ],
    ids=["explicit", "agent-configured", "inherited-agent", "default"],
)
def test_agy_is_spawn_capable_across_selection_paths(
    explicit_provider: str | None,
    agent_provider: str | None,
    default_provider: str | None,
) -> None:
    resolved: str | None
    try:
        resolved = resolve_spawn_provider(
            explicit_provider=explicit_provider,
            agent_provider=agent_provider,
            default_provider=default_provider,
        )
    except ValueError:
        resolved = None
    assert resolved == "agy"


def test_explicit_provider_precedes_agent_and_default() -> None:
    assert (
        resolve_spawn_provider(
            explicit_provider="codex",
            agent_provider="qwen",
            default_provider="droid",
        )
        == "codex"
    )


def test_concrete_agent_provider_precedes_default() -> None:
    assert (
        resolve_spawn_provider(
            explicit_provider=None,
            agent_provider="grok",
            default_provider="codex",
        )
        == "grok"
    )


def test_provider_aliases_are_normalized() -> None:
    assert concrete_provider(" OpenAI ") == "codex"
    assert concrete_provider("anthropic") == "claude"


def test_supplied_model_requires_explicit_provider() -> None:
    error = missing_provider_for_supplied_model(
        explicit_provider=None,
        model="grok-4.6",
    )
    assert error is not None
    assert error.error_code == PROVIDER_REQUIRED_FOR_MODEL
    assert error.model == "grok-4.6"


def test_supplied_model_accepts_explicit_provider() -> None:
    assert (
        missing_provider_for_supplied_model(
            explicit_provider="grok",
            model="grok-4.6",
        )
        is None
    )


def test_model_omitted_does_not_require_provider_argument() -> None:
    assert (
        missing_provider_for_supplied_model(
            explicit_provider=None,
            model=None,
        )
        is None
    )


class _EmptyStore:
    def get_provider_snapshot(self, provider: str) -> None:
        return None


class _SnapshotStore:
    def __init__(self, snapshots: dict[str, ProviderSnapshot]) -> None:
        self._snapshots = snapshots

    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None:
        return self._snapshots.get(provider)


class _NoMetadata:
    def get_context_window(self, model: str) -> None:
        return None

    def get_model_metadata(self, model: str) -> None:
        return None


def _model_capability(name: str) -> ModelCapability:
    return ModelCapability(
        canonical_model=name,
        display_name=name,
        aliases=(),
        available=True,
        hidden=False,
        is_default=False,
        context_length=None,
        max_output_tokens=None,
        reasoning=ReasoningSupport.UNKNOWN,
        supported_efforts=None,
        default_effort=None,
        latency_class=None,
        input_modalities=None,
        supports_tools=None,
        provenance={},
    )


def test_generation_endpoint_model_skips_catalog_check() -> None:
    resolver = CapabilityResolver(_EmptyStore(), _NoMetadata())
    assert (
        incompatible_spawn_model_provider(
            provider="codex",
            model="endpoint:lm-studio",
            resolver=resolver,
        )
        is None
    )


def test_unknown_model_passes_when_provider_catalog_is_missing() -> None:
    resolver = CapabilityResolver(_EmptyStore(), _NoMetadata())
    assert (
        incompatible_spawn_model_provider(
            provider="codex",
            model="grok-4.6",
            resolver=resolver,
        )
        is None
    )


def test_codex_catalog_rejects_grok_model_and_names_serving_providers() -> None:
    resolver = CapabilityResolver(
        _SnapshotStore(
            {
                "codex": ProviderSnapshot(
                    provider="codex",
                    generation=1,
                    models=(_model_capability("gpt-5.6-luna"),),
                    sources=(),
                ),
                "grok": ProviderSnapshot(
                    provider="grok",
                    generation=1,
                    models=(_model_capability("grok-4.6"),),
                    sources=(),
                ),
            }
        ),
        _NoMetadata(),
    )
    error = incompatible_spawn_model_provider(
        provider="codex",
        model="grok-4.6",
        resolver=resolver,
    )
    assert error is not None
    assert error.error_code == INCOMPATIBLE_MODEL_PROVIDER
    assert error.provider == "codex"
    assert error.model == "grok-4.6"
    assert error.compatible_providers == ("grok",)


@pytest.mark.parametrize("model", [None, "", " ", "inherit", " INHERIT "])
def test_inherited_model_uses_provider_default(model: str | None) -> None:
    assert _normalize_optional_model(model) is None


def test_concrete_model_override_is_preserved() -> None:
    assert _normalize_optional_model("gpt-5.6-sol") == "gpt-5.6-sol"


def test_parent_session_provider_reads_session_source() -> None:
    manager = SimpleNamespace(get=lambda session_id: SimpleNamespace(source=f"{session_id}-source"))

    assert parent_session_provider(manager, "codex") == "codex-source"


@pytest.mark.parametrize("manager", [None, object(), SimpleNamespace(get=lambda _key: None)])
def test_parent_session_provider_tolerates_unavailable_context(manager: object | None) -> None:
    assert parent_session_provider(manager, "missing") is None


def _session_manager(sources: dict[str, str | None]) -> SimpleNamespace:
    return SimpleNamespace(get=lambda session_id: SimpleNamespace(source=sources.get(session_id)))


def test_spawning_session_provider_prefers_caller_over_declared_parent() -> None:
    manager = _session_manager({"adversary": "codex", "coordinator": "claude"})

    inherited = spawning_session_provider(
        manager,
        caller_session_id="adversary",
        parent_session_id="coordinator",
    )

    assert inherited == "codex"


def test_spawning_session_provider_falls_back_to_parent_without_caller() -> None:
    manager = _session_manager({"coordinator": "claude"})

    inherited = spawning_session_provider(
        manager,
        caller_session_id=None,
        parent_session_id="coordinator",
    )

    assert inherited == "claude"


@pytest.mark.parametrize("caller_source", [None, "", "inherit"])
def test_spawning_session_provider_falls_back_when_caller_source_is_unresolved(
    caller_source: str | None,
) -> None:
    manager = _session_manager({"worker": caller_source, "coordinator": "codex"})

    inherited = spawning_session_provider(
        manager,
        caller_session_id="worker",
        parent_session_id="coordinator",
    )

    assert inherited == "codex"


def test_spawning_session_provider_returns_none_without_any_source() -> None:
    assert (
        spawning_session_provider(None, caller_session_id="caller", parent_session_id="parent")
        is None
    )


def test_agent_worker_inherits_spawning_agent_provider_not_coordinator() -> None:
    """A codex agent spawning an inherit-provider worker keeps the worker on codex."""
    manager = _session_manager({"adversary": "codex", "coordinator": "claude"})

    resolved = resolve_spawn_provider(
        explicit_provider=None,
        agent_provider="inherit",
        default_provider=spawning_session_provider(
            manager,
            caller_session_id="adversary",
            parent_session_id="coordinator",
        ),
    )

    assert resolved == "codex"
