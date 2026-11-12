"""Bundled definitions declare a concrete model, pair their reviewers, and resolve seat pins.

Every check walks the bundled YAML generically. This file names no bundled definition, pipeline
or rule: a definition is looked up by the name its YAML declares, never by a name written here.
"""

import re
from typing import Any

import pytest
import yaml

from gobby.agents.model_defaults import ModelDefaults, is_denylisted, model_defaults_from_policy
from gobby.agents.sync import get_bundled_agents_path
from gobby.mcp_proxy.tools.spawn_agent._launch_settings import (
    agent_provider_for_spawn,
    resolve_launch_model,
)
from gobby.mcp_proxy.tools.spawn_agent._provider_resolution import resolve_spawn_provider
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_models import PipelineDefinition
from gobby.workflows.sync_pipelines import get_bundled_pipelines_path
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit

# The one definition name that carries no model by design.
UNPINNED_DEFAULT = "default"
# Model ids that no bundled choice, primary or fallback, may name.
RETIRED_MODELS = frozenset({"gpt-5.6-sol"})
# The project's default_model_denylist (.gobby/workflows/rules/project-operating-policy.yaml).
DENYLISTED_FAMILIES = ["fable", "astra", "argon", "carbon"]
# Capability tier per model token. A reviewer's tier must be at least its author's.
TIER_BY_TOKEN = {"haiku": 1, "terra": 1, "sonnet": 2, "opus": 3, "sol": 3, "grok": 3}
PIN_REFERENCE = re.compile(r"\$\{\{\s*inputs\.(\w+)\s*\}\}")
# The spawn arguments a seat pin feeds into the launch resolver.
SEAT_ARGUMENTS = ("agent", "provider", "model", "reasoning_effort")


def _load_bodies() -> dict[str, AgentDefinitionBody]:
    bodies: dict[str, AgentDefinitionBody] = {}
    for path in sorted(get_bundled_agents_path().rglob("*.yaml")):
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{path.name} is not a YAML mapping"
        data.setdefault("name", path.stem)
        body = AgentDefinitionBody.model_validate(data)
        bodies[body.name] = body
    return bodies


def _load_pipelines() -> list[tuple[str, PipelineDefinition]]:
    pipelines: list[tuple[str, PipelineDefinition]] = []
    for path in sorted(get_bundled_pipelines_path().glob("*.yaml")):
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        pipelines.append((path.stem, PipelineDefinition.model_validate(data)))
    return pipelines


BODIES = _load_bodies()
PIPELINES = _load_pipelines()
CONCRETE = sorted(name for name in BODIES if name != UNPINNED_DEFAULT)
REVIEWERS = sorted(name for name, body in BODIES.items() if body.reviews)


def _input_default(spec: Any) -> Any:
    return spec.get("default") if isinstance(spec, dict) else None


def _seat_pin_params() -> list[Any]:
    """Every spawn step whose model is a bare pipeline input, with that input's default."""
    params: list[Any] = []
    for stem, pipeline in PIPELINES:
        defaults = {name: _input_default(spec) for name, spec in pipeline.inputs.items()}
        for step in pipeline.steps:
            mcp = step.mcp
            if mcp is None or mcp.server != "gobby-agents" or mcp.tool != "spawn_agent":
                continue
            arguments = mcp.arguments or {}
            model = arguments.get("model")
            pin = PIN_REFERENCE.fullmatch(model.strip()) if isinstance(model, str) else None
            if pin is None:
                continue
            params.append(
                pytest.param(
                    arguments,
                    defaults,
                    pin.group(1),
                    id=f"{stem}:{step.id}",
                )
            )
    return params


SEAT_PINS = _seat_pin_params()


def _tokens(model: str) -> list[str]:
    base = model.split("[", 1)[0].lower()
    return [token for token in re.split(r"[-._/:]", base) if token]


def _tier(model: str) -> int:
    tiers = {TIER_BY_TOKEN[token] for token in _tokens(model) if token in TIER_BY_TOKEN}
    if len(tiers) != 1:
        raise ValueError(f"model {model!r} has no single known capability tier")
    return tiers.pop()


def test_bundled_definitions_are_discovered() -> None:
    assert CONCRETE
    assert REVIEWERS
    assert SEAT_PINS


def test_unpinned_default_declares_no_model() -> None:
    body = BODIES[UNPINNED_DEFAULT]
    assert body.provider == "inherit"
    assert body.model is None


@pytest.mark.parametrize("name", CONCRETE)
def test_bundled_definition_declares_its_launch_model(name: str) -> None:
    body = BODIES[name]
    assert body.provider != "inherit"
    assert body.model
    assert body.reasoning_effort
    assert body.fallbacks


@pytest.mark.parametrize("name", sorted(BODIES))
def test_no_bundled_choice_is_retired_or_denylisted(name: str) -> None:
    body = BODIES[name]
    choices = [body.model, *(fallback.model for fallback in body.fallbacks)]
    for model in (choice for choice in choices if choice is not None):
        assert model not in RETIRED_MODELS, f"{name} names retired model {model!r}"
        assert not is_denylisted(model, DENYLISTED_FAMILIES), (
            f"{name} names denylisted model {model!r}"
        )


def test_tier_rejects_a_model_with_no_known_tier() -> None:
    with pytest.raises(ValueError):
        _tier("mystery-7")


@pytest.mark.parametrize("reviewer", REVIEWERS)
def test_reviewer_pairs_cross_provider_at_or_above_author_tier(reviewer: str) -> None:
    body = BODIES[reviewer]
    assert body.model is not None
    for author_name in body.reviews:
        assert author_name in BODIES, f"{reviewer} reviews unknown seat {author_name!r}"
        author = BODIES[author_name]
        assert author.model is not None
        assert body.provider != author.provider, f"{reviewer} shares a provider with {author_name}"
        assert _tier(body.model) >= _tier(author.model), (
            f"{reviewer} is a lower tier than {author_name}"
        )


@pytest.mark.parametrize(("arguments", "input_defaults", "pin"), SEAT_PINS)
def test_seat_model_input_defaults_to_null(
    arguments: dict[str, Any], input_defaults: dict[str, Any], pin: str
) -> None:
    assert input_defaults.get(pin) is None, "a seat model pin defaults to null so policy decides"


@pytest.mark.parametrize(("arguments", "input_defaults", "pin"), SEAT_PINS)
async def test_null_seat_model_input_resolves_to_the_policy_entry(
    arguments: dict[str, Any], input_defaults: dict[str, Any], pin: str
) -> None:
    agent = arguments["agent"]
    assert agent in BODIES, f"seat pin names an unknown definition {agent!r}"
    body = BODIES[agent]
    marker = f"map-marker-{agent}"
    policy: ModelDefaults = model_defaults_from_policy(
        {"agent_model_defaults": {agent: {"provider": body.provider, "model": marker}}}
    )
    context = {
        "inputs": input_defaults,
        "_string_inputs": list(input_defaults),
        "steps": {},
    }
    seat = {key: arguments[key] for key in SEAT_ARGUMENTS if key in arguments}

    rendered = StepRenderer(TemplateEngine()).render_mcp_arguments(seat, context)
    assert rendered.get("model") is None

    provider = resolve_spawn_provider(
        explicit_provider=rendered.get("provider"),
        agent_provider=agent_provider_for_spawn(body, rendered.get("model"), policy),
        default_provider=None,
    )
    launch = await resolve_launch_model(
        provider=provider,
        model=rendered.get("model"),
        agent_body=body,
        reasoning_effort=rendered.get("reasoning_effort"),
        reasoning_required=None,
        model_defaults=policy,
    )
    assert launch.model == marker
