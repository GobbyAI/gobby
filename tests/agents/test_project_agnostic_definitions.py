"""Bundled roles consume project policy and launch assignments without fixing a roster."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.workflows.definitions import AgentDefinitionBody, PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit

RUN_SPECIFICS = re.compile(
    r"\b(?:Josh|Story B|gobby-telegram|0\.5\.0)\b|#\d{5}\b|\bgobby#\d+\b"
    r"|\bLanes?\s+\d|04:45|06:45"
    r"|\b(?:close backlog|heavy-run|heavy slots)\s*(?:cap)?\s*[:/]?\s*\d"
    r"|\babove\s+54\b|\bbelow\s+36\b",
    re.IGNORECASE,
)


def load_definition(name: str) -> AgentDefinitionBody:
    raw = cast(
        dict[str, Any], yaml.safe_load((get_bundled_agents_path() / f"{name}.yaml").read_text())
    )
    return AgentDefinitionBody.model_validate(raw)


def test_bundled_roles_and_runbooks_have_no_project_literals() -> None:
    agents = get_bundled_agents_path()
    roots = (agents, agents.parent / "pipelines")
    findings = [
        f"{path.relative_to(agents.parent)}:{line_number}: {match.group()}"
        for root in roots
        for path in sorted(root.rglob("*.yaml"))
        for line_number, line in enumerate(path.read_text().splitlines(), 1)
        for match in RUN_SPECIFICS.finditer(line)
    ]
    assert findings == [], "\n".join(findings)


def test_bundled_roles_and_runbooks_inherit_provider_choices() -> None:
    agents = get_bundled_agents_path()
    for path in sorted(agents.rglob("*.yaml")):
        raw = cast(dict[str, Any], yaml.safe_load(path.read_text()))
        for key in ("provider", "model", "reasoning_effort"):
            assert raw.get(key) in (None, "inherit"), (path.name, key, raw.get(key))
    for path in sorted((agents.parent / "pipelines").glob("team-*.yaml")):
        raw = cast(dict[str, Any], yaml.safe_load(path.read_text()))
        for name, spec in raw["inputs"].items():
            if name.endswith(("_provider", "_model", "_reasoning_effort")):
                assert spec.get("default") is None, (path.name, name)


@pytest.mark.parametrize("lane_count", [3, 9])
def test_reporter_renders_every_lane_from_policy(lane_count: int) -> None:
    lanes = [
        {"name": f"Lane {number}", "epic": f"#{10000 + number}"}
        for number in range(1, lane_count + 1)
    ]
    rendered = load_definition("reporter").prompt_for(
        "agent", variables={"project_policy": {"lanes": lanes}}
    )
    blocks = re.findall(r"^- (Lane \d+) \((#\d+)\):", rendered, re.MULTILINE)
    assert blocks == [(lane["name"], lane["epic"]) for lane in lanes]
    assert "{{" not in rendered


@pytest.mark.parametrize(
    "name", ["developer", "code-reviewer", "security-reviewer", "researcher", "lane-manager"]
)
def test_launch_assignment_is_rendered_in_the_seat_definition(name: str) -> None:
    assignment = {
        "lane": "verification",
        "lane_epic": "#42",
        "worktree": "/tmp/assigned-checkout",
        "first_job": "#43 Verify configuration",
    }
    rendered = load_definition(name).prompt_for("agent", variables={"seat_context": assignment})
    for value in assignment.values():
        assert value in rendered
    assert "{{" not in rendered


def test_missing_or_cleared_policy_keeps_the_reporter_role_and_reports_the_gap() -> None:
    reporter = load_definition("reporter")
    default_prompt = reporter.prompt_for("agent")
    cleared_prompt = reporter.prompt_for("agent", variables={"project_policy": None})
    assert cleared_prompt == default_prompt
    assert "You own the scheduled status" in cleared_prompt
    assert "mark missing policy as a source gap" in cleared_prompt


def test_team_lane_passes_assignment_inputs_to_each_spawn() -> None:
    path = get_bundled_agents_path().parent / "pipelines" / "team-lane.yaml"
    definition = PipelineDefinition.model_validate(yaml.safe_load(path.read_text()))
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(lane="03", lane_epic="project#42", first_job="Audit routing")
    context = {
        "inputs": inputs,
        "steps": {"guard": {"output": {"worktree": "resolved-worktree"}}},
        "invocation_id": "assignment-run",
        "_string_inputs": frozenset(
            name for name, spec in definition.inputs.items() if spec.get("type") == "string"
        ),
    }
    renderer = StepRenderer(TemplateEngine())
    spawns = [step.mcp for step in definition.steps if step.mcp and step.mcp.tool == "spawn_agent"]
    assert len(spawns) == 5
    for spawn in spawns:
        assert spawn is not None and spawn.arguments is not None
        rendered = renderer.render_mcp_arguments(
            {"seat_context": spawn.arguments["seat_context"]}, context, drop_none=True
        )
        assert rendered["seat_context"] == {
            "lane": "03",
            "lane_epic": "project#42",
            "worktree": "resolved-worktree",
            "first_job": "Audit routing",
        }


def test_project_policy_lanes_are_the_injected_lane_inventory() -> None:
    path = (
        Path(__file__).resolve().parents[2] / ".gobby/workflows/rules/project-operating-policy.yaml"
    )
    raw = cast(dict[str, Any], yaml.safe_load(path.read_text()))
    effects = raw["rules"]["project-operating-policy"]["effects"]
    policy = next(
        effect["value"] for effect in effects if effect.get("variable") == "project_policy"
    )
    template = next(effect["template"] for effect in effects if effect["type"] == "inject_context")
    rendered = TemplateEngine().render(template, {"variables": {"project_policy": policy}})
    for lane in policy["lanes"]:
        assert f"{lane['number']}. {lane['description']}" in rendered
    assert len(policy["lanes"]) == 9
    assert policy["integration_branch"] == "0.5.0"
