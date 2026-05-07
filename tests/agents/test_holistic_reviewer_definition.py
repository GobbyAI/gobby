"""Phase 2 contract tests for the holistic-reviewer agent definition."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def _agent() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/holistic-reviewer.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_three_outcomes() -> None:
    agent = _agent()
    review_step = next(step for step in agent["steps"] if step["name"] == "review")
    success_tools = {item["tool"] for item in review_step.get("on_mcp_success", [])}

    assert {
        "complete_stage",
        "fail_stage",
        "escalate_task",
    } <= success_tools


def test_success_path_uses_complete_stage_for_in_progress_holistic_qa() -> None:
    agent = _agent()
    review_step = next(step for step in agent["steps"] if step["name"] == "review")
    blocked = set(review_step["blocked_mcp_tools"])
    instructions = agent["instructions"]
    status = review_step["status_message"]

    assert "complete_stage" in instructions
    assert 'stage_name="holistic_qa"' in instructions
    assert "validation_override_reason" in instructions
    assert 'complete_stage(stage_name="holistic_qa"' in status
    assert "gobby-tasks-ops:approve_review" in blocked
    assert "gobby-tasks-ops:reject_review" in blocked
    assert "gobby-agents:end_agent_run" in blocked
    assert (
        "gobby-agents:end_agent_run"
        in next(step for step in agent["steps"] if step["name"] == "terminate")["allowed_mcp_tools"]
    )


def test_reads_subtree() -> None:
    agent = _agent()
    claim_step = next(step for step in agent["steps"] if step["name"] == "claim")
    review_text = next(step for step in agent["steps"] if step["name"] == "review")[
        "status_message"
    ]

    assert "gobby-tasks:get_task" in claim_step["allowed_mcp_tools"]
    assert "gobby-tasks:list_tasks" in claim_step["allowed_mcp_tools"]
    assert "list_tasks(parent_task_id=" in agent["instructions"]
    assert "list_tasks(parent_task_id=assigned_task_id)" in review_text


def test_docs_epics_can_use_discovery_brief_plan_substitute() -> None:
    agent = _agent()
    instructions = agent["instructions"]
    status = next(step for step in agent["steps"] if step["name"] == "review")[
        "status_message"
    ]

    assert "Discovery Brief" in instructions
    assert "descendant task set" in instructions
    assert "task references" in instructions
    assert "Discovery Brief" in status
    assert "plan substitute" in status


def test_holistic_review_order_is_spec_quality_testing_yagni() -> None:
    agent = _agent()
    instructions = agent["instructions"]
    status = next(step for step in agent["steps"] if step["name"] == "review")["status_message"]

    ordered = ["spec_compliance", "code_quality", "testing", "yagni"]
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert instructions.index(earlier) < instructions.index(later)
    assert "operational_risk" not in instructions
    assert "aggregate diff" in status


def test_loads_required_skills_before_review() -> None:
    agent = _agent()
    steps = {step["name"]: step for step in agent["steps"]}
    load_step = steps["load_skill"]

    assert agent["step_variables"]["required_skills"] == [
        "holistic-review",
        "tech-writer",
    ]
    assert load_step["allowed_mcp_tools"] == ["gobby-skills:get_skill"]
    assert 'get_skill(name="holistic-review")' in load_step["status_message"]
    assert 'get_skill(name="tech-writer")' in load_step["status_message"]
    assert load_step["transitions"] == [
        {
            "to": "review",
            "when": "all(skill in vars.get('loaded_skills', []) for skill in vars.required_skills)",
        }
    ]

    success = load_step["on_mcp_success"][0]
    assert success["server"] == "gobby-skills"
    assert success["tool"] == "get_skill"
    assert success["variable"] == "required_skills_loaded"
