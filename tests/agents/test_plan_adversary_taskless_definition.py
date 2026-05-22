"""Contract tests for taskless plan-adversary agent definition."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

AGENT_PATH = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml"
)


def _agent() -> dict[str, Any]:
    data = yaml.safe_load(AGENT_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_taskless_adversary_has_no_task_lifecycle_claim() -> None:
    agent = _agent()
    step_names = [step["name"] for step in agent["steps"]]
    text = yaml.safe_dump(agent)

    assert "claim" not in step_names
    assert "assigned_task_id" not in text
    review_step = next(step for step in agent["steps"] if step["name"] == "review")
    blocked = set(review_step["blocked_mcp_tools"])
    assert "gobby-tasks:claim_task" in blocked
    assert "gobby-tasks:claim_task" not in set(review_step.get("allowed_mcp_tools") or [])
    assert "gobby-tasks-ops:approve_review" in blocked
    assert "gobby-tasks-ops:reject_review" in blocked


def test_taskless_adversary_loads_plan_review_and_reports_structured_result() -> None:
    agent = _agent()
    steps = {step["name"]: step for step in agent["steps"]}

    assert steps["load_skill"]["allowed_mcp_tools"] == ["gobby-skills:get_skill"]
    assert 'get_skill(name="plan-review")' in steps["load_skill"]["status_message"]
    assert "structured" in steps["review"]["description"].lower()
    assert "verdict" in steps["review"]["status_message"].lower()
    assert "## V1 Plan Changelog" in agent["instructions"]
    assert "## M1 Task Manifest" in agent["instructions"]
    assert "implementation_domain" in agent["instructions"]
    assert "gobby-agents:end_agent_run" in steps["review"]["allowed_mcp_tools"]
