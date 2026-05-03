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

    assert review_step.get("allowed_tools") != "all"
    allowed_tools = set(review_step.get("allowed_tools", []))
    blocked_mcp_tools = set(review_step.get("blocked_mcp_tools", []))
    assert not {"Edit", "Write"} & allowed_tools
    assert "gobby-tasks:close_task" in blocked_mcp_tools


def test_emits_review_verdict() -> None:
    agent = _agent()
    instructions = agent["instructions"]
    review_step = next(step for step in agent["steps"] if step["name"] == "review")
    success_tools = {item["tool"] for item in review_step.get("on_mcp_success", [])}
    allowed_mcp_tools = set(review_step.get("allowed_mcp_tools", []))

    assert "approve_review" in instructions
    assert "reject_review" in instructions
    assert {"approve_review", "reject_review"} <= success_tools
    assert {
        "gobby-tasks-ops:approve_review",
        "gobby-tasks-ops:reject_review",
    } <= allowed_mcp_tools


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
