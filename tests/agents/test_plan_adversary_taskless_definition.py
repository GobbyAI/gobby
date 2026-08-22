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
    step_names = [step["name"] for step in agent["step_workflow"]["steps"]]
    text = yaml.safe_dump(agent)

    assert "claim" not in step_names
    assert "assigned_task_id" not in text
    review_step = next(step for step in agent["step_workflow"]["steps"] if step["name"] == "review")
    blocked = set(review_step["blocked_mcp_tools"])
    assert "gobby-tasks:claim_task" in blocked
    assert "gobby-tasks:claim_task" not in set(review_step.get("allowed_mcp_tools") or [])
    assert "gobby-tasks-ops:approve_review" in blocked
    assert "gobby-tasks-ops:reject_review" in blocked
    assert "gobby-plans:derive_plan_handoff_manifest" in blocked
    assert "gobby-plans:apply_plan_handoff_manifest" in blocked


def test_taskless_adversary_loads_plan_review_and_reports_structured_result() -> None:
    agent = _agent()
    steps = {step["name"]: step for step in agent["step_workflow"]["steps"]}

    assert steps["load_skill"]["allowed_mcp_tools"] == [
        "gobby-skills:get_skill",
        "gobby-plans:get_plan_review_snapshot",
    ]
    status = steps["load_skill"]["status_message"]
    assert "plan-review" in status
    assert any(
        tool_name in status
        for tool_name in ("get_skill", "list_tools", "get_tool_schema", "call_tool")
    )
    assert "proxy tools" in status
    assert "native Skill" in status
    assert "GitHub/app connector" in status
    assert "Computer Use tools" in status
    assert "structured" in steps["review"]["description"].lower()
    assert "verdict" in steps["review"]["status_message"].lower()
    assert "After the workflow has advanced to `review`" in agent["prompts"]["agent"]
    assert "## V1 Plan Changelog" in agent["prompts"]["agent"]
    assert "## M1 Task Manifest" in agent["prompts"]["agent"]
    assert "implementation_domain" in agent["prompts"]["agent"]
    assert "PLAN IDENTITY PRECONDITION" in agent["prompts"]["agent"]
    assert "**Plan ID:** <id>" in agent["prompts"]["agent"]
    assert "covers:unknown:" in agent["prompts"]["agent"]
    assert "gobby-agents:end_agent_run" in steps["review"]["allowed_mcp_tools"]


def test_taskless_adversary_loads_proportionality() -> None:
    # Plan-altitude proportionality (anti-Rube-Goldberg) is loaded alongside
    # plan-review so the taskless interactive adversary applies the same
    # over-engineering criterion as the stage-native agent.
    agent = _agent()
    steps = {step["name"]: step for step in agent["step_workflow"]["steps"]}

    status = steps["load_skill"]["status_message"]
    assert "proportionality" in status
    assert 'call_tool("gobby-skills", "get_skill", {"name": "proportionality"})' in status

    load_success = steps["load_skill"]["on_mcp_success"]
    variables = {entry.get("variable") for entry in load_success}
    assert "skill_loaded" in variables
    assert "proportionality_loaded" in variables

    transition_whens = " ".join(
        t.get("when", "") for t in steps["load_skill"].get("transitions", [])
    )
    assert "proportionality_loaded" in transition_whens

    instructions = agent["prompts"]["agent"]
    assert "proportionality" in instructions
    assert "over-engineering" in instructions
    assert "simpler form" in instructions


def test_taskless_adversary_review_step_allows_send_message_to_parent() -> None:
    """Regression for #15100.

    The plan-review methodology requires the adversary to `send_message` its
    structured verdict + findings back to the parent on `verdict: needs_review`.
    Because `allowed_mcp_tools` is a
    whitelist, omitting `gobby-agents:send_message` implicitly blocks the
    call and the parent never sees rejection-round findings (observed via
    run-5231d2f026de which completed `success` after 57 turns without
    delivering any verdict).
    """
    agent = _agent()
    review_step = next(step for step in agent["step_workflow"]["steps"] if step["name"] == "review")
    allowed = set(review_step["allowed_mcp_tools"])
    assert "gobby-agents:send_message" in allowed, (
        "send_message must be in the review step's allowed_mcp_tools whitelist "
        "so the adversary can deliver structured findings to its parent."
    )


def test_taskless_adversary_returns_exact_result_for_coordinator_persistence() -> None:
    agent = _agent()
    review_step = next(step for step in agent["step_workflow"]["steps"] if step["name"] == "review")

    assert "gobby-memory:create_memory" not in review_step["allowed_mcp_tools"]
    assert "gobby-agents:send_message" in review_step["allowed_mcp_tools"]
    assert "gobby-agents:end_agent_run" in review_step["allowed_mcp_tools"]
    status_message = review_step["status_message"]
    assert "Send that exact JSON with send_message" in status_message
    assert "then call end_agent_run" in status_message


def test_rejection_template_carries_location_and_repairs() -> None:
    agent = _agent()
    instructions = agent["prompts"]["agent"]
    review_step = next(step for step in agent["step_workflow"]["steps"] if step["name"] == "review")

    assert "gobby-plans:apply_plan_review_repairs" in set(agent["blocked_mcp_tools"])
    assert "gobby-plans:apply_plan_review_repairs" in set(review_step["blocked_mcp_tools"])

    template_start = instructions.index("verdict: needs_review")
    template_end = instructions.index("```", template_start)
    template = instructions[template_start:template_end]
    assert "location:" in template
    assert "repairs:" in template
    for kind in ("add_targets", "add_dependency", "add_acceptance"):
        assert f"kind: {kind}" in template
    prose = " ".join(instructions.split())
    assert "Repair class vs design class" in prose
    assert "Never apply a repair yourself" in prose
