"""Contracts for architect-owned test architecture."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

pytestmark = pytest.mark.unit


ROOT = Path(__file__).resolve().parents[2]


def _agent(name: str) -> dict[str, Any]:
    path = ROOT / f"src/gobby/install/shared/workflows/agents/{name}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return cast(dict[str, Any], data)


def _step(agent: dict[str, Any], name: str) -> dict[str, Any]:
    steps = cast(list[dict[str, Any]], agent["steps"])
    matches = [step for step in steps if step["name"] == name]
    assert len(matches) == 1
    return matches[0]


def test_standalone_test_architect_definition_removed() -> None:
    path = ROOT / "src/gobby/install/shared/workflows/agents/test-architect.yaml"

    assert not path.exists()


def test_architect_loads_test_architecture_methodology() -> None:
    agent = _agent("architect")
    load_skill = _step(agent, "load_skill")
    draft = _step(agent, "draft")
    success_hooks = load_skill.get("on_mcp_success", [])

    assert agent["name"] == "architect"
    assert {"architecture", "test-architecture"}.issubset(set(agent["skills"]["methodology"]))
    assert "## Architecture Brief" in agent["instructions"]
    assert "## Test Architecture" in agent["instructions"]
    assert 'get_skill(name="test-architecture")' in load_skill["status_message"]
    assert any(hook["when"] == "tool_input.name == 'test-architecture'" for hook in success_hooks)
    assert "## Test Architecture" in draft["status_message"]


def test_test_architecture_skill_outputs_structured_prose_not_expansion_tasks() -> None:
    skill_path = ROOT / "src/gobby/install/shared/skills/test-architecture/SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")

    assert "## Test Architecture" in skill_text
    for heading in (
        "### Integration",
        "### E2E",
        "### Regression",
        "### Contract",
        "### Infrastructure",
    ):
        assert heading in skill_text
    assert "Do NOT write `### N.N` task sections" in skill_text
    assert "[category: test] leaves" in skill_text
