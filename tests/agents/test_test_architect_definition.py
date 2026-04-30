"""Phase 2 contract tests for the test-architect agent definition."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

pytestmark = pytest.mark.unit


def _agent() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/test-architect.yaml"
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return cast(dict[str, Any], data)


def _step(agent: dict[str, Any], name: str) -> dict[str, Any]:
    steps = cast(list[dict[str, Any]], agent["steps"])
    matches = [step for step in steps if step["name"] == name]
    assert len(matches) == 1
    return matches[0]


def test_definition_loads() -> None:
    agent = _agent()

    assert agent["name"] == "test-architect"
    assert "spawn" in agent["surfaces"]
    assert any(step["name"] == "design" for step in agent["steps"])


def test_design_step_enforces_test_architect_contract() -> None:
    agent = _agent()
    design = _step(agent, "design")
    blocked_tools = set(design.get("blocked_mcp_tools", []))
    success_tools = {item["tool"] for item in design.get("on_mcp_success", [])}

    assert "mark_task_review_approved" in agent["instructions"]
    assert "gobby-tasks:close_task" in blocked_tools
    assert "gobby-agents:spawn_agent" in blocked_tools
    assert "gobby-agents:dispatch_batch" in blocked_tools
    assert "mark_task_review_approved" in success_tools
    assert "escalate_task" in success_tools
