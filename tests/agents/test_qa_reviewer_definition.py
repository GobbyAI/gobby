"""Phase 2 contract tests for the qa-reviewer agent definition."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def _agent() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/qa-reviewer.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_no_write_permissions() -> None:
    agent = _agent()
    review_step = next(step for step in agent["steps"] if step["name"] == "review")
    terminate_step = next(step for step in agent["steps"] if step["name"] == "terminate")
    instructions = agent["instructions"]

    assert review_step.get("allowed_tools") != "all"
    allowed_tools = set(review_step.get("allowed_tools", []))
    blocked_mcp_tools = set(review_step.get("blocked_mcp_tools", []))
    terminate_allowed_mcp_tools = set(terminate_step.get("allowed_mcp_tools", []))
    assert not {"Edit", "Write"} & allowed_tools
    assert "gobby-tasks:close_task" in blocked_mcp_tools
    assert "gobby-agents:kill_agent" in blocked_mcp_tools
    assert "gobby-agents:kill_agent" not in terminate_allowed_mcp_tools
    assert "gobby-agents:end_agent_run" in terminate_allowed_mcp_tools
    assert "end_agent_run" in instructions
    assert "Call kill_agent" not in instructions


def test_emits_review_verdict() -> None:
    agent = _agent()
    instructions = agent["instructions"]
    review_step = next(step for step in agent["steps"] if step["name"] == "review")
    status_message = review_step["status_message"]
    success_tools = {item["tool"] for item in review_step.get("on_mcp_success", [])}
    allowed_mcp_tools = set(review_step.get("allowed_mcp_tools", []))

    assert "approve_review" in instructions
    assert "reject_review" in instructions
    assert "call that verdict tool immediately" in instructions
    assert "artifacts, wait for mutexes" in instructions
    assert "task_id=assigned_task_id" in status_message
    assert "Dispatcher lifecycle owns post-verdict merge/closure" in status_message
    assert {"approve_review", "reject_review"} <= success_tools
    assert {
        "gobby-tasks-ops:approve_review",
        "gobby-tasks-ops:reject_review",
    } <= allowed_mcp_tools


def test_escalation_is_limited_to_broken_workflow() -> None:
    agent = _agent()
    instructions = agent["instructions"]
    review_step = next(step for step in agent["steps"] if step["name"] == "review")
    status_message = review_step["status_message"]

    assert 'Use escalate_task only for "my workflow is broken" failures' in instructions
    assert "Missing/incorrect implementation behavior is always" in instructions
    assert "reject_review, not escalation" in instructions
    assert "only if your workflow is broken" in status_message
    assert 'only for "my workflow is broken" tooling failures' in status_message


def test_loads_required_skills_before_review() -> None:
    agent = _agent()
    steps = {step["name"]: step for step in agent["steps"]}
    claim_step = steps["claim"]
    load_step = steps["load_skills"]

    assert agent["step_variables"]["required_skills"] == [
        "code-index",
        "task-transitions",
        "verification-before-completion",
    ]
    assert claim_step["transitions"] == [{"to": "load_skills", "when": "vars.task_claimed"}]
    assert load_step["allowed_mcp_tools"] == ["gobby-skills:get_skill"]
    assert "code-index" in load_step["status_message"]
    assert "task-transitions" in load_step["status_message"]
    assert "verification-before-completion" in load_step["status_message"]
    assert "Do not call claim_task" in load_step["status_message"]
    assert 'get_skill(name="code-index")' in load_step["status_message"]
    assert 'get_skill(name="task-transitions")' in load_step["status_message"]
    assert 'get_skill(name="verification-before-completion")' in load_step["status_message"]
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


def test_auto_claimed_reviewers_do_not_reclaim() -> None:
    agent = _agent()
    instructions = agent["instructions"]

    assert "If the active workflow step is already past CLAIM" in instructions
    assert "do not call claim_task again" in instructions


def test_leaf_review_is_ordered_by_spec_then_quality() -> None:
    agent = _agent()
    instructions = agent["instructions"]
    review_step = next(step for step in agent["steps"] if step["name"] == "review")
    status_message = review_step["status_message"]

    assert instructions.index("spec_compliance") < instructions.index("code_quality")
    assert status_message.index("spec_compliance") < status_message.index("code_quality")
    assert "Reject immediately" in instructions
    assert "Reject before code_quality" in status_message
    assert "only when both" in instructions
