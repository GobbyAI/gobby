"""Launch settings refuse a spawn with no model and default reasoning to medium (#25168)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from gobby.agents import reasoning
from gobby.mcp_proxy.tools.spawn_agent._launch_settings import LaunchRefused, resolve_launch_model
from gobby.providers.capabilities.models import (
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
)
from gobby.providers.capabilities.resolve import CapabilityResolver
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

_MODEL = "claude-sonnet-5"


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
    capability = ModelCapability(
        canonical_model=_MODEL,
        display_name=_MODEL,
        aliases=(),
        available=True,
        hidden=False,
        is_default=True,
        context_length=None,
        max_output_tokens=None,
        reasoning=ReasoningSupport.KNOWN,
        supported_efforts=("low", "medium", "high"),
        default_effort=None,
        latency_class=None,
        input_modalities=None,
        supports_tools=None,
        provenance={},
    )
    snapshot = ProviderSnapshot(provider="claude", generation=1, models=(capability,), sources=())
    resolver = CapabilityResolver(_Store(snapshot), _NoMetadata())
    monkeypatch.setattr(reasoning, "_get_capability_resolver", lambda: resolver)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "with_definition", [False, True], ids=["no-definition", "definition-without-model"]
)
async def test_spawn_without_a_model_is_refused_naming_the_model(
    build_agent_body: Callable[..., AgentDefinitionBody], with_definition: bool
) -> None:
    body = build_agent_body(name="seat", provider="claude") if with_definition else None

    with pytest.raises(LaunchRefused) as excinfo:
        await resolve_launch_model(
            provider="claude",
            model=None,
            agent_body=body,
            reasoning_effort=None,
            reasoning_required=None,
        )

    assert excinfo.value.response["success"] is False
    assert "model" in str(excinfo.value.response["error"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "with_definition", [False, True], ids=["no-definition", "definition-with-model"]
)
async def test_spawn_without_reasoning_effort_runs_at_medium(
    claude_reasoning: None,
    build_agent_body: Callable[..., AgentDefinitionBody],
    with_definition: bool,
) -> None:
    body = (
        build_agent_body(name="seat", provider="claude", model=_MODEL) if with_definition else None
    )

    launch = await resolve_launch_model(
        provider="claude",
        model=_MODEL,
        agent_body=body,
        reasoning_effort=None,
        reasoning_required=None,
    )

    assert launch.reasoning_effort == "medium"
    assert launch.reasoning.effective_effort == "medium"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spawn_effort", "definition_effort", "expected"),
    [("high", None, "high"), (None, "low", "low")],
    ids=["spawn-argument", "definition"],
)
async def test_explicit_reasoning_effort_is_not_replaced_by_the_default(
    claude_reasoning: None,
    build_agent_body: Callable[..., AgentDefinitionBody],
    spawn_effort: str | None,
    definition_effort: str | None,
    expected: str,
) -> None:
    body = build_agent_body(
        name="seat",
        provider="claude",
        model=_MODEL,
        reasoning_effort=definition_effort,
    )

    launch = await resolve_launch_model(
        provider="claude",
        model=_MODEL,
        agent_body=body,
        reasoning_effort=spawn_effort,
        reasoning_required=None,
    )

    assert launch.reasoning_effort == expected
