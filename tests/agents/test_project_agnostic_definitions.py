"""Bundled roles consume project policy and launch assignments without fixing a roster."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.llm.sdk_utils import ADDITIONAL_CONTEXT_LIMIT, fit_additional_context
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit

RUN_SPECIFICS = re.compile(
    r"\b(?:Josh|Story B|gobby-telegram|0\.5\.0)\b|#\d{5}\b|\bgobby#\d+\b"
    r"|\bLanes?\s+\d|04:45|06:45"
    r"|\b(?:close backlog|heavy-run|heavy slots)\s*(?:cap)?\s*[:/]?\s*\d"
    r"|\babove\s+54\b|\bbelow\s+36\b",
    re.IGNORECASE,
)

PROCEDURE_ROLES = [
    "code-reviewer",
    "lane-manager",
    "orchestrator",
    "reporter",
    "task-close-reviewer",
]


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
        assert raw.get("provider") in (None, "inherit"), path.name
        if path.name == "inbox-manager.yaml":
            assert raw.get("model") == "claude-sonnet-5-5"
            assert raw.get("reasoning_effort") == "medium"
        else:
            for key in ("model", "reasoning_effort"):
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


def test_project_policy_lane_inventory_and_role_guidance_stay_queryable() -> None:
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
    assert 'get_variable(name="project_policy")' in rendered
    assert "role_guidance before work" in rendered
    assert "project_policy.handoff_reason_template" in rendered
    assert [lane["number"] for lane in policy["lanes"]] == list(range(1, 10))
    for lane in policy["lanes"]:
        assert lane["name"]
        assert lane["epic"].startswith("#")
        assert lane["description"]
    assert len(policy["lanes"]) == 9
    assert policy["integration_branch"] == "0.5.0"
    assert policy["launch_defaults"]["team-lane"]["developer_provider"] == "codex"
    assert policy["role_launch_defaults"]["expansion-qa"]["model"] == "gpt-6-luna"
    assert {
        "analyst",
        "architect",
        "product-manager",
        "qa-reviewer",
        "doc-reviewer",
        "trajectory-monitor",
        "deprecated/qa-dev",
        "planner",
        "plan-adversary-old",
        "plan-adversary-taskless-old",
        "plan-enhancer-old",
        "plan-enhancer-taskless-old",
    }.isdisjoint(policy["role_launch_defaults"])
    assert "ROADMAP progress and the Story B critical path" in policy["role_guidance"]["reporter"]
    assert "all plan-expanded epic subtrees" in policy["role_guidance"]["consolidator"]
    assert policy["handoff_reason_template"].startswith("Handed off: reviewer LAND at <sha>")


def _render_project_policy() -> str:
    path = (
        Path(__file__).resolve().parents[2] / ".gobby/workflows/rules/project-operating-policy.yaml"
    )
    raw = cast(dict[str, Any], yaml.safe_load(path.read_text()))
    effects = raw["rules"]["project-operating-policy"]["effects"]
    policy = next(
        effect["value"] for effect in effects if effect.get("variable") == "project_policy"
    )
    template = next(effect["template"] for effect in effects if effect["type"] == "inject_context")
    return TemplateEngine().render(template, {"variables": {"project_policy": policy}})


def test_project_policy_contribution_fits_provider_context_limit() -> None:
    rendered = _render_project_policy()

    assert len(rendered) <= ADDITIONAL_CONTEXT_LIMIT


def test_project_policy_fits_with_shared_guidance_and_seat_prompt() -> None:
    path = get_bundled_agents_path().parent / "rules/roles/inject-seat-common.yaml"
    raw = cast(dict[str, Any], yaml.safe_load(path.read_text()))
    effects = raw["rules"]["inject-seat-common"]["effects"]
    common = next(effect["template"] for effect in effects if effect["type"] == "inject_context")
    parts = [
        ("agent_prompt", load_definition("orchestrator").prompt_for("persona")),
        ("rule:inject-seat-common", common),
        ("rule:project-operating-policy", _render_project_policy()),
    ]

    fitted = fit_additional_context(parts)

    assert fitted.omitted == []
    assert fitted.text == "\n\n".join(text for _, text in parts)
    assert len(fitted.text) <= ADDITIONAL_CONTEXT_LIMIT


@pytest.mark.parametrize(
    "name", [path.stem for path in sorted(get_bundled_agents_path().glob("*.yaml"))]
)
def test_every_bundled_agent_fits_the_per_turn_policy_budget(name: str) -> None:
    policy_path = (
        Path(__file__).resolve().parents[2] / ".gobby/workflows/rules/project-operating-policy.yaml"
    )
    policy_raw = cast(dict[str, Any], yaml.safe_load(policy_path.read_text()))
    policy_effects = policy_raw["rules"]["project-operating-policy"]["effects"]
    policy = next(
        effect["value"] for effect in policy_effects if effect.get("variable") == "project_policy"
    )
    common_path = get_bundled_agents_path().parent / "rules/roles/inject-seat-common.yaml"
    common_raw = cast(dict[str, Any], yaml.safe_load(common_path.read_text()))
    common_effects = common_raw["rules"]["inject-seat-common"]["effects"]
    common = next(
        effect["template"] for effect in common_effects if effect["type"] == "inject_context"
    )
    definition = load_definition(name)
    variables = {
        "project_policy": policy,
        "seat_context": {
            "lane": "1",
            "lane_epic": "project#42",
            "worktree": "/tmp/assigned-checkout",
            "first_job": "project#43 Verify configuration",
        },
    }
    for surface in definition.surfaces:
        prompt = definition.prompt_for(
            "agent" if surface == "spawn" else "persona", variables=variables
        )
        parts = [
            ("agent_prompt", prompt),
            ("rule:inject-seat-common", common),
            ("rule:project-operating-policy", _render_project_policy()),
        ]

        fitted = fit_additional_context(parts)

        assert fitted.omitted == [], f"{name}/{surface} omitted context"
        assert len(fitted.text) <= ADDITIONAL_CONTEXT_LIMIT


@pytest.mark.parametrize("name", PROCEDURE_ROLES)
def test_moved_seat_procedures_are_required_in_every_context_epoch(name: str) -> None:
    shared = get_bundled_agents_path().parents[1]
    rule_path = shared / "workflows/rules/skill-discovery/require-seat-procedure.yaml"
    assert rule_path.is_file(), "Moved procedures need an enforced load gate"
    raw = cast(dict[str, Any], yaml.safe_load(rule_path.read_text()))
    rule = raw["rules"]["require-seat-procedure"]
    assert rule["enabled"] and rule["event"] == "before_tool"
    assert rule["effects"][0]["type"] == "block"

    reference = f"gobby:references/agents/procedures/{name}.md"
    definition = load_definition(name)
    assert definition.workflows.variables["_seat_procedure"] == reference
    content = (shared / "skills/gobby" / reference.split(":", 1)[1]).read_text()
    assert not RUN_SPECIFICS.search(content)
    assert "{{" not in content and "{%" not in content
    for surface in definition.surfaces:
        prompt = definition.prompt_for("agent" if surface == "spawn" else "persona")
        assert reference.split(":", 1)[1] in prompt
        assert "after compaction" in prompt

    includes = definition.workflows.rule_selectors.include
    assert "name:require-seat-procedure" in includes or (
        "tag:default" in includes and "default" in raw["tags"]
    )
    if definition.step_workflow and "required_skills" in definition.step_workflow.variables:
        assert reference in definition.step_workflow.variables["required_skills"]
    variables: dict[str, Any] = {"_seat_procedure": reference, "loaded_skill_references": []}
    event = SimpleNamespace(data={"tool_name": "Bash", "tool_input": {"command": "git status"}})
    context = {"variables": variables, "event": event, "tool_input": event.data["tool_input"]}
    evaluator = SafeExpressionEvaluator(context, build_condition_helpers(context=context))
    assert evaluator.evaluate(rule["when"]), "Work must wait for the complete reference load"
    variables["loaded_skills"] = [reference]
    assert evaluator.evaluate(rule["when"]), "A skill entrypoint cannot replace its reference"
    variables["loaded_skill_references"] = [reference]
    assert not evaluator.evaluate(rule["when"])
    variables["loaded_skill_references"] = []  # Context loss clears tracked reference loads.
    assert evaluator.evaluate(rule["when"]), "Compaction must require the reference again"
    for server, tool in [
        ("gobby", "get_tool_schema"),
        ("gobby-skills", "get_skill_file"),
        ("gobby-results", "get_tool_result"),
        ("gobby-sessions", "get_handoff"),
        ("gobby-sessions", "set_handoff"),
        ("gobby-memory", "get_memory"),
        ("gobby-agents", "send_message"),
        ("gobby-agents", "wait_for_coordination"),
    ]:
        event.data = {"mcp_server": server, "mcp_tool": tool}
        assert not evaluator.evaluate(rule["when"]), f"Recovery must remain available: {tool}"


def test_lane_manager_procedure_keeps_author_owned_retest_proof() -> None:
    shared = get_bundled_agents_path().parents[1]
    procedure = shared / "skills/gobby/references/agents/procedures/lane-manager.md"
    content = procedure.read_text()

    for protocol in ("PROOF_RESULT", "UNAVAILABLE"):
        assert protocol in content, f"lane-manager must retain the current {protocol} contract"
    assert "The author lane owns the landed retest" in content
    # The Orchestrator lands and reports LANDED; the author owns the retest.
    assert "PROOF_REQUEST" not in content
    assert "run them in the main checkout" not in content
    assert "the reviewer has run the retest" not in content
    assert "confirm it with the author before raising found work" in content
