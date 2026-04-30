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
    assert not {"Edit", "Write"} & allowed_tools


def test_emits_review_verdict() -> None:
    agent = _agent()
    instructions = agent["instructions"]
    review_step = next(step for step in agent["steps"] if step["name"] == "review")
    success_tools = {item["tool"] for item in review_step.get("on_mcp_success", [])}

    assert "mark_task_review_approved" in instructions
    assert "mark_task_review_rejected" in instructions
    assert {"mark_task_review_approved", "mark_task_review_rejected"} <= success_tools
