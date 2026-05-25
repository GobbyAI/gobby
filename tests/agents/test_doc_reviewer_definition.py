"""Contract tests for the doc-reviewer agent definition."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def _agent() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/doc-reviewer.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_doc_reviewer_is_read_only() -> None:
    agent = _agent()
    review_step = next(step for step in agent["steps"] if step["name"] == "review")
    terminate_step = next(step for step in agent["steps"] if step["name"] == "terminate")

    allowed_tools = set(review_step.get("allowed_tools", []))
    blocked_mcp_tools = set(review_step.get("blocked_mcp_tools", []))
    terminate_allowed_mcp_tools = set(terminate_step.get("allowed_mcp_tools", []))

    assert review_step.get("allowed_tools") != "all"
    assert not {"Edit", "Write"} & allowed_tools
    assert "gobby-tasks:close_task" in blocked_mcp_tools
    assert "gobby-agents:kill_agent" in blocked_mcp_tools
    assert "gobby-agents:end_agent_run" in terminate_allowed_mcp_tools
    assert "read-only" in agent["instructions"]


def test_doc_reviewer_loads_required_skills() -> None:
    agent = _agent()
    load_step = next(step for step in agent["steps"] if step["name"] == "load_skills")

    assert agent["step_variables"]["required_skills"] == [
        "code-index",
        "tech-writer",
        "task-transitions",
    ]
    assert load_step["allowed_mcp_tools"] == ["gobby-skills:get_skill"]
    for skill_name in agent["step_variables"]["required_skills"]:
        assert f'get_skill(name="{skill_name}")' in load_step["status_message"]
    assert "Do not call claim_task" in load_step["status_message"]


def test_doc_reviewer_uses_ordered_docs_review_and_verdict_tools() -> None:
    agent = _agent()
    instructions = agent["instructions"]
    review_step = next(step for step in agent["steps"] if step["name"] == "review")
    status_message = review_step["status_message"]
    allowed_mcp_tools = set(review_step.get("allowed_mcp_tools", []))
    success_tools = {item["tool"] for item in review_step.get("on_mcp_success", [])}

    assert instructions.index("docs_spec_compliance") < instructions.index("docs_quality")
    assert status_message.index("docs_spec_compliance") < status_message.index("docs_quality")
    assert 'stage_name="development"' in instructions
    assert "approve_review" in instructions
    assert "reject_review" in instructions
    assert "escalate_task" in instructions
    assert "After successful final validation in REVIEW" in instructions
    assert "pending terminal-verdict obligation" in instructions
    assert "After successful final validation" in status_message
    assert {
        "gobby-tasks-ops:approve_review",
        "gobby-tasks-ops:reject_review",
        "gobby-tasks:escalate_task",
    } <= allowed_mcp_tools
    assert {"approve_review", "reject_review", "escalate_task"} <= success_tools
