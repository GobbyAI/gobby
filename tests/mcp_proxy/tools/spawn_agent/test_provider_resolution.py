"""Provider-neutral spawn routing tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.mcp_proxy.tools.spawn_agent._provider_resolution import (
    concrete_provider,
    parent_session_provider,
    resolve_spawn_provider,
    spawning_session_provider,
)
from gobby.mcp_proxy.tools.spawn_agent._runtime import _normalize_optional_model

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("provider", ["claude", "codex", "droid", "grok", "qwen"])
def test_inherit_uses_spawn_capable_parent_provider(provider: str) -> None:
    assert (
        resolve_spawn_provider(
            explicit_provider="inherit",
            agent_provider="inherit",
            parent_provider=provider,
        )
        == provider
    )


@pytest.mark.parametrize("provider", [None, "", "inherit", "agy", "pipeline", "unknown"])
def test_inherit_falls_back_for_unsupported_parent_provider(provider: str | None) -> None:
    assert (
        resolve_spawn_provider(
            explicit_provider=None,
            agent_provider="inherit",
            parent_provider=provider,
        )
        == "claude"
    )


def test_explicit_provider_precedes_agent_and_parent() -> None:
    assert (
        resolve_spawn_provider(
            explicit_provider="codex",
            agent_provider="qwen",
            parent_provider="droid",
        )
        == "codex"
    )


def test_concrete_agent_provider_precedes_parent() -> None:
    assert (
        resolve_spawn_provider(
            explicit_provider=None,
            agent_provider="grok",
            parent_provider="codex",
        )
        == "grok"
    )


def test_provider_aliases_are_normalized() -> None:
    assert concrete_provider(" OpenAI ") == "codex"
    assert concrete_provider("anthropic") == "claude"


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
        parent_provider=spawning_session_provider(
            manager,
            caller_session_id="adversary",
            parent_session_id="coordinator",
        ),
    )

    assert resolved == "codex"
