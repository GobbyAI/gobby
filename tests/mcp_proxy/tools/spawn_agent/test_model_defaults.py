"""Model defaults: launch arg > project-policy map > definition > refuse (#25641)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.agents import reasoning
from gobby.agents.model_defaults import (
    ModelChoice,
    ModelDefaults,
    is_denylisted,
    model_defaults_from_policy,
)
from gobby.mcp_proxy.tools.spawn_agent import _launch_settings
from gobby.mcp_proxy.tools.spawn_agent._launch_settings import (
    LaunchModel,
    LaunchRefused,
    agent_provider_for_spawn,
    load_model_defaults,
    resolve_launch_model,
)
from gobby.providers.capabilities.models import (
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
)
from gobby.providers.capabilities.resolve import CapabilityResolver
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

_MODEL = "claude-sonnet-5"
_MAP_MODEL = "claude-opus-5-5"
_DEF_MODEL = "claude-haiku-5-5"
_FALLBACK = "claude-sonnet-5-5"
_DENIED = "claude-fable-5-1"


class _Store:
    def __init__(self, snapshot: ProviderSnapshot) -> None:
        self._snapshot = snapshot

    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None:
        return self._snapshot if provider == self._snapshot.provider else None


class _NoMetadata:
    def get_context_window(self, model: str) -> None:
        return None

    def get_model_metadata(self, model: str) -> None:
        return None


@pytest.fixture
def claude_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    models = tuple(
        ModelCapability(
            canonical_model=name,
            display_name=name,
            aliases=(),
            available=True,
            hidden=False,
            is_default=name == _MODEL,
            context_length=None,
            max_output_tokens=None,
            reasoning=ReasoningSupport.KNOWN,
            supported_efforts=("low", "medium", "high", "xhigh"),
            default_effort=None,
            latency_class=None,
            input_modalities=None,
            supports_tools=None,
            provenance={},
        )
        for name in (_MODEL, _MAP_MODEL, _DEF_MODEL, _FALLBACK, _DENIED)
    )
    snapshot = ProviderSnapshot(provider="claude", generation=1, models=models, sources=())
    resolver = CapabilityResolver(_Store(snapshot), _NoMetadata())
    monkeypatch.setattr(reasoning, "_get_capability_resolver", lambda: resolver)


def _policy(
    entries: Mapping[str, Mapping[str, Any]], denylist: list[str] | None = None
) -> ModelDefaults:
    return model_defaults_from_policy(
        {"agent_model_defaults": dict(entries), "default_model_denylist": denylist or []}
    )


def _entry(model: str, **extra: Any) -> dict[str, Any]:
    return {"provider": "claude", "model": model, "reasoning_effort": "high", **extra}


async def _resolve(
    body: AgentDefinitionBody | None,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    defaults: ModelDefaults | None = None,
    provider: str = "claude",
) -> LaunchModel:
    return await resolve_launch_model(
        provider=provider,
        model=model,
        agent_body=body,
        reasoning_effort=reasoning_effort,
        reasoning_required=None,
        model_defaults=defaults,
    )


@pytest.mark.asyncio
async def test_policy_map_beats_definition_model(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude", model=_DEF_MODEL)

    launch = await _resolve(body, defaults=_policy({"seat-alpha": _entry(_MAP_MODEL)}))

    assert launch.model == _MAP_MODEL


@pytest.mark.asyncio
async def test_launch_arg_beats_policy_map(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude", model=_DEF_MODEL)

    launch = await _resolve(
        body, model=_MODEL, defaults=_policy({"seat-alpha": _entry(_MAP_MODEL)})
    )

    assert launch.model == _MODEL
    assert launch.model_chain[0].model == _MODEL


@pytest.mark.asyncio
async def test_definition_model_is_used_when_map_has_no_entry(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude", model=_DEF_MODEL)

    launch = await _resolve(body, defaults=_policy({"seat-beta": _entry(_MAP_MODEL)}))

    assert launch.model == _DEF_MODEL


@pytest.mark.asyncio
async def test_map_effort_beats_definition_effort(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(
        name="seat-alpha", provider="claude", model=_DEF_MODEL, reasoning_effort="low"
    )
    defaults = _policy({"seat-alpha": _entry(_MAP_MODEL, reasoning_effort="xhigh")})

    launch = await _resolve(body, defaults=defaults)

    assert launch.reasoning_effort == "xhigh"


@pytest.mark.asyncio
async def test_definition_effort_applies_when_map_has_no_effort(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(
        name="seat-alpha", provider="claude", model=_DEF_MODEL, reasoning_effort="low"
    )
    defaults = _policy({"seat-alpha": {"provider": "claude", "model": _MAP_MODEL}})

    launch = await _resolve(body, defaults=defaults)

    assert launch.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_no_model_anywhere_is_refused(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude")

    with pytest.raises(LaunchRefused) as excinfo:
        await _resolve(body, defaults=_policy({}))

    assert "model" in str(excinfo.value.response["error"])


@pytest.mark.asyncio
async def test_denylisted_policy_model_is_refused_as_a_default(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude")
    defaults = _policy({"seat-alpha": _entry(_DENIED)}, denylist=["fable"])

    with pytest.raises(LaunchRefused) as excinfo:
        await _resolve(body, defaults=defaults)

    assert "denylist" in str(excinfo.value.response["error"])


@pytest.mark.asyncio
async def test_denylisted_definition_model_is_refused_as_a_default(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude", model=_DENIED)

    with pytest.raises(LaunchRefused) as excinfo:
        await _resolve(body, defaults=_policy({}, denylist=["fable"]))

    assert "denylist" in str(excinfo.value.response["error"])


@pytest.mark.asyncio
async def test_denylisted_explicit_launch_arg_is_allowed(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude")

    launch = await _resolve(body, model=_DENIED, defaults=_policy({}, denylist=["fable"]))

    assert launch.model == _DENIED


@pytest.mark.asyncio
async def test_denylist_does_not_match_name_substrings(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude")
    defaults = _policy({"seat-alpha": _entry(_MAP_MODEL)}, denylist=["fable"])

    launch = await _resolve(body, defaults=defaults)

    assert launch.model == _MAP_MODEL


def test_is_denylisted_matches_name_tokens_and_bracket_suffixes() -> None:
    assert is_denylisted("claude-fable-5-1[1m]", ["fable"])
    assert is_denylisted("gpt-6.1-astra", ["astra"])
    assert not is_denylisted("fabled-model", ["fable"])
    assert not is_denylisted("claude-opus-5-5", [])


@pytest.mark.asyncio
async def test_fallbacks_are_exposed_in_order_after_the_primary(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude")
    fallbacks = [
        {"provider": "claude", "model": _FALLBACK, "reasoning_effort": "medium"},
        {"provider": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "xhigh"},
    ]
    defaults = _policy({"seat-alpha": _entry(_MAP_MODEL, fallbacks=fallbacks)})

    launch = await _resolve(body, defaults=defaults)

    assert [(c.provider, c.model, c.reasoning_effort) for c in launch.model_chain] == [
        ("claude", _MAP_MODEL, "high"),
        ("claude", _FALLBACK, "medium"),
        ("codex", "gpt-6.1-sol", "xhigh"),
    ]


@pytest.mark.asyncio
async def test_chain_without_policy_entry_is_only_the_primary(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude", model=_DEF_MODEL)

    launch = await _resolve(body, defaults=_policy({}))

    assert launch.model_chain == (
        ModelChoice(provider="claude", model=_DEF_MODEL, reasoning_effort="medium"),
    )


@pytest.mark.asyncio
async def test_denylisted_fallback_refuses_the_chain(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude")
    fallbacks = [{"provider": "codex", "model": "gpt-6.1-fable", "reasoning_effort": "xhigh"}]
    defaults = _policy({"seat-alpha": _entry(_MAP_MODEL, fallbacks=fallbacks)}, denylist=["fable"])

    with pytest.raises(LaunchRefused) as excinfo:
        await _resolve(body, defaults=defaults)

    assert "denylist" in str(excinfo.value.response["error"])


@pytest.mark.asyncio
async def test_default_agent_without_model_refuses_with_guidance(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="default", provider="inherit")
    defaults = _policy({"default": _entry(_MAP_MODEL)})

    with pytest.raises(LaunchRefused) as excinfo:
        await _resolve(body, defaults=defaults)

    error = str(excinfo.value.response["error"])
    assert "by design" in error
    assert "provider" in error
    assert "model" in error
    assert "reasoning_effort" in error


@pytest.mark.asyncio
async def test_default_agent_with_explicit_model_launches(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="default", provider="inherit")

    launch = await _resolve(body, model=_MODEL, defaults=_policy({}))

    assert launch.model == _MODEL


def test_policy_parses_map_and_denylist() -> None:
    fallbacks = [{"provider": "claude", "model": _FALLBACK}]
    defaults = _policy(
        {"seat-alpha": _entry(_MAP_MODEL, fallbacks=fallbacks)},
        denylist=["fable", "astra"],
    )

    assert defaults.denylist == ("fable", "astra")
    assert defaults.entries["seat-alpha"].model == _MAP_MODEL
    assert defaults.entries["seat-alpha"].fallbacks[0].model == _FALLBACK


def test_absent_policy_keys_yield_empty_defaults() -> None:
    defaults = model_defaults_from_policy({})

    assert defaults.entries == {}
    assert defaults.denylist == ()


def test_policy_map_entry_without_model_is_rejected() -> None:
    with pytest.raises(ValueError):
        model_defaults_from_policy({"agent_model_defaults": {"seat-alpha": {"provider": "claude"}}})


def test_policy_map_entry_with_unknown_key_is_rejected() -> None:
    entry = {"model": _MAP_MODEL, "tier": "low"}

    with pytest.raises(ValueError):
        model_defaults_from_policy({"agent_model_defaults": {"seat-alpha": entry}})


def test_legacy_launch_default_keys_are_not_read() -> None:
    defaults = model_defaults_from_policy(
        {"launch_defaults": {"dev-team": {"developer_model": _MAP_MODEL}}}
    )

    assert defaults.entries == {}


@pytest.mark.asyncio
async def test_map_entry_for_another_provider_does_not_apply(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude", model=_DEF_MODEL)
    defaults = _policy({"seat-alpha": _entry(_MAP_MODEL, provider="codex")})

    launch = await _resolve(body, defaults=defaults)

    assert launch.model == _DEF_MODEL


@pytest.mark.asyncio
async def test_launch_arg_model_takes_no_policy_fallbacks_or_effort(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude")
    fallbacks = [{"provider": "claude", "model": _FALLBACK}]
    defaults = _policy({"seat-alpha": _entry(_MAP_MODEL, fallbacks=fallbacks)})

    launch = await _resolve(body, model=_MODEL, defaults=defaults)

    assert launch.model_chain == (
        ModelChoice(provider="claude", model=_MODEL, reasoning_effort="medium"),
    )


@pytest.mark.asyncio
async def test_fallback_without_effort_takes_the_primary_effort(
    claude_reasoning: None, build_agent_body: Callable[..., AgentDefinitionBody]
) -> None:
    body = build_agent_body(name="seat-alpha", provider="claude")
    fallbacks = [{"provider": "codex", "model": "gpt-6.1-sol"}]
    defaults = _policy(
        {"seat-alpha": _entry(_MAP_MODEL, reasoning_effort="xhigh", fallbacks=fallbacks)}
    )

    launch = await _resolve(body, defaults=defaults)

    assert launch.model_chain[1] == ModelChoice(
        provider="codex", model="gpt-6.1-sol", reasoning_effort="xhigh"
    )


def test_policy_provider_applies_only_when_the_spawn_names_no_model(
    build_agent_body: Callable[..., AgentDefinitionBody],
) -> None:
    body = build_agent_body(name="seat-alpha", provider="inherit")
    defaults = _policy({"seat-alpha": _entry(_MAP_MODEL, provider="codex")})

    assert agent_provider_for_spawn(body, None, defaults) == "codex"
    assert agent_provider_for_spawn(body, _MODEL, defaults) == "inherit"
    assert agent_provider_for_spawn(None, None, defaults) is None


class _RuleStore:
    def __init__(self, rules: list[SimpleNamespace]) -> None:
        self._rules = rules

    def list_effective(self, project_id: str) -> list[SimpleNamespace]:
        return self._rules

    def list_all(self, *, project_id: str) -> list[SimpleNamespace]:
        return self._rules


def _policy_rule(
    value: dict[str, Any], *, name: str = "project-operating-policy"
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        source="installed",
        enabled=True,
        definition_json={
            "effects": [{"type": "set_variable", "variable": "project_policy", "value": value}]
        },
    )


def test_loader_reads_the_installed_policy_map(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _policy_rule(
        {
            "agent_model_defaults": {"seat-alpha": {"provider": "claude", "model": _MAP_MODEL}},
            "default_model_denylist": ["fable"],
        }
    )
    monkeypatch.setattr(_launch_settings, "RuleDefinitionManager", lambda db: _RuleStore([rule]))

    defaults = load_model_defaults(object(), "project-1")

    assert defaults.entries["seat-alpha"].model == _MAP_MODEL
    assert defaults.denylist == ("fable",)


def test_loader_refuses_conflicting_installed_policies(monkeypatch: pytest.MonkeyPatch) -> None:
    rules = [
        _policy_rule({"default_model_denylist": ["fable"]}, name="first"),
        _policy_rule({"default_model_denylist": []}, name="second"),
    ]
    monkeypatch.setattr(_launch_settings, "RuleDefinitionManager", lambda db: _RuleStore(rules))

    with pytest.raises(ValueError):
        load_model_defaults(object(), "project-1")


def test_loader_refuses_a_malformed_map(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _policy_rule({"agent_model_defaults": {"seat-alpha": {"provider": "claude"}}})
    monkeypatch.setattr(_launch_settings, "RuleDefinitionManager", lambda db: _RuleStore([rule]))

    with pytest.raises(ValueError):
        load_model_defaults(object(), "project-1")


def test_loader_without_a_database_is_empty() -> None:
    assert load_model_defaults(None, None) == ModelDefaults()
