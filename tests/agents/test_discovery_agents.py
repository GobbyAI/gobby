"""Contract tests for discovery-stage bundled agents and methodology skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents._yaml_helpers import _field, find_step

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "src/gobby/install/shared/workflows/agents"
SKILLS_DIR = REPO_ROOT / "src/gobby/install/shared/skills"

DISCOVERY_AGENTS = {
    "analyst": {
        "stage": "ideation",
        "skill": "ideate",
        "section": "Discovery Brief",
    },
    "researcher": {
        "stage": "research",
        "skill": "research",
        "section": "Research Findings",
    },
    "architect": {
        "stage": "architecture",
        "skill": "architecture",
        "section": "Architecture Brief",
    },
    "product-manager": {
        "stage": "prd",
        "skill": "prd",
        "section": "Product Reference Document",
    },
}

REQUIRED_STAGE_MCP_TOOLS = {
    "gobby-tasks:claim_task",
    "gobby-tasks:get_task",
    "gobby-tasks:update_task",
    "gobby-skills:get_skill",
    "gobby-tasks-ops:complete_stage",
    "gobby-tasks:escalate_task",
}


def _agent_path(slug: str) -> Path:
    return AGENTS_DIR / f"{slug}.yaml"


def _raw_agent(slug: str) -> dict[str, Any]:
    payload = yaml.safe_load(_agent_path(slug).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _agent(slug: str) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(_raw_agent(slug))


def _skill_text(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def _allowed_mcp_union(agent: AgentDefinitionBody) -> set[str]:
    tools: set[str] = set()
    for step in agent.steps or []:
        allowed = step.allowed_mcp_tools
        assert allowed != "all"
        tools.update(allowed)
    return tools


@pytest.mark.parametrize(("slug", "spec"), DISCOVERY_AGENTS.items())
def test_discovery_agent_yaml_validates_and_is_enabled(slug: str, spec: dict[str, str]) -> None:
    raw_text = _agent_path(slug).read_text(encoding="utf-8")
    raw = _raw_agent(slug)
    agent = _agent(slug)

    assert "PLACEHOLDER" not in raw_text
    assert "_bmad" not in raw_text.lower()
    assert ".claude/bmad-skills" not in raw_text
    assert agent.name == slug
    assert agent.enabled is True
    assert agent.provider == "codex"
    assert agent.model == "gpt-5.5"
    assert agent.reasoning_effort == "high"
    assert agent.isolation == "none"
    assert agent.surfaces == ["spawn"]
    assert raw["skills"]["methodology"] == [spec["skill"]]
    assert f"Gobby acting as {slug.replace('-', ' ')}" in (agent.instructions or "")


@pytest.mark.parametrize(("slug", "spec"), DISCOVERY_AGENTS.items())
def test_discovery_agent_loads_expected_methodology_skill(
    slug: str,
    spec: dict[str, str],
) -> None:
    agent = _agent(slug)

    assert [step.name for step in (agent.steps or [])] == ["claim", "load_skill", "draft"]
    assert agent.exit_condition == "vars.handoff_ready"

    load_step = find_step(agent.steps or [], "load_skill")
    assert load_step is not None
    assert load_step.allowed_mcp_tools == ["gobby-skills:get_skill"]
    assert spec["skill"] in (load_step.status_message or "")

    mcp_success = getattr(load_step, "on_mcp_success", []) or []
    assert (
        "gobby-skills",
        "get_skill",
        "skill_loaded",
        f"tool_input.name == '{spec['skill']}'",
    ) in {
        (
            _field(entry, "server"),
            _field(entry, "tool"),
            _field(entry, "variable"),
            _field(entry, "when"),
        )
        for entry in mcp_success
    }


@pytest.mark.parametrize(("slug", "spec"), DISCOVERY_AGENTS.items())
def test_discovery_agent_marker_and_stage_contract(slug: str, spec: dict[str, str]) -> None:
    agent = _agent(slug)
    instructions = agent.instructions or ""
    stage = spec["stage"]

    assert f"gobby:discovery-stage:{stage}:start" in instructions
    assert f"gobby:discovery-stage:{stage}:end" in instructions
    assert f"## {spec['section']}" in instructions
    assert "update_task" in instructions
    assert "complete_stage" in instructions
    assert f'stage_name="{stage}"' in instructions
    assert "close_task" in instructions
    assert "spawn other agents" in instructions

    draft = find_step(agent.steps or [], "draft")
    assert draft is not None
    assert draft.allowed_mcp_tools == [
        "gobby-tasks:get_task",
        "gobby-tasks:update_task",
        "gobby-tasks:escalate_task",
        "gobby-tasks-ops:complete_stage",
    ]

    success_tools = {
        (_field(entry, "server"), _field(entry, "tool"), _field(entry, "variable"))
        for entry in (getattr(draft, "on_mcp_success", None) or [])
    }
    assert ("gobby-tasks-ops", "complete_stage", "handoff_ready") in success_tools
    assert ("gobby-tasks", "escalate_task", "handoff_ready") in success_tools


@pytest.mark.parametrize(("slug", "spec"), DISCOVERY_AGENTS.items())
def test_discovery_agent_mcp_allowlist_is_stage_scoped(slug: str, spec: dict[str, str]) -> None:
    agent = _agent(slug)

    assert _allowed_mcp_union(agent) == REQUIRED_STAGE_MCP_TOOLS
    assert spec["stage"] in (agent.instructions or "")


@pytest.mark.parametrize(("slug", "spec"), DISCOVERY_AGENTS.items())
def test_discovery_methodology_skill_exists(slug: str, spec: dict[str, str]) -> None:
    text = _skill_text(spec["skill"])

    assert f"name: {spec['skill']}" in text
    assert "internal: true" in text
    assert f"## {spec['section']}" in text
    assert "methodology" in text.lower()


def test_planner_treats_discovery_markers_as_authoritative_context() -> None:
    planner = _raw_agent("planner")
    instructions = planner["instructions"]

    assert "gobby:discovery-stage:*:start" in instructions
    assert "authoritative upstream context" in instructions
    assert "planning handoff notes" in instructions
