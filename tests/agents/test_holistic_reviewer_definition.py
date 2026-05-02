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
        "approve_review",
        "reject_review",
        "escalate_task",
    } <= success_tools


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
