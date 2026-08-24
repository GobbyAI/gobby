"""Contract tests for the read-only trajectory-monitor agent."""

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

AGENT_PATH = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/workflows/agents/trajectory-monitor.yaml"
)

pytestmark = pytest.mark.unit


def _agent() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(AGENT_PATH.read_text(encoding="utf-8")))


def _step(agent: dict[str, Any], name: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        next(step for step in agent["step_workflow"]["steps"] if step["name"] == name),
    )


def test_trajectory_monitor_is_read_only_and_terminates_explicitly() -> None:
    agent = _agent()
    review = _step(agent, "review")
    terminate = _step(agent, "terminate")

    assert agent["name"] == "trajectory-monitor"
    assert agent["step_workflow"]["variables"]["verdict_emitted"] is False
    assert {"Edit", "Write", "NotebookEdit"}.isdisjoint(review["allowed_tools"])
    assert "gobby-tasks:close_task" in review["blocked_mcp_tools"]
    assert terminate["allowed_mcp_tools"] == ["gobby-agents:end_agent_run"]
    assert agent["step_workflow"]["exit_condition"] == "current_step == 'terminate'"


def test_trajectory_monitor_reviews_linked_and_cumulative_branch_history() -> None:
    review = _step(_agent(), "review")
    status = review["status_message"]
    allowed = set(review["allowed_mcp_tools"])

    assert "gobby-tasks:get_task_diff" in allowed
    assert "gobby-tasks:get_task_stages" in allowed
    assert "gobby-tasks-ops:get_artifacts" in allowed
    assert "gobby-tasks-ops:get_delivery_state" in allowed
    assert "gobby-worktrees:get_worktree_by_task" in allowed
    assert "gobby-clones:get_clone_by_task" in allowed
    assert "merge-base" in status
    assert "target_branch" in status
    assert "linked commit set" in status
    assert "post-approval delta" in status
    assert "task description and plan scope" in status


def test_trajectory_monitor_emits_one_pr_verdict_or_suspicion_escalation() -> None:
    agent = _agent()
    review = _step(agent, "review")
    allowed = set(review["allowed_mcp_tools"])
    status = review["status_message"]

    assert "gobby-tasks-ops:approve_review" in allowed
    assert "gobby-tasks-ops:reject_review" in allowed
    assert "gobby-tasks:escalate_task" in allowed
    assert 'stage_name="pr"' in status
    assert "trajectory_suspicious:" in status
    verdict_effects = {
        (effect["server"], effect["tool"])
        for effect in review["on_mcp_success"]
        if effect.get("variable") == "verdict_emitted"
    }
    assert verdict_effects == {
        ("gobby-tasks-ops", "approve_review"),
        ("gobby-tasks-ops", "reject_review"),
        ("gobby-tasks", "escalate_task"),
    }
    assert review["transitions"] == [
        {"to": "terminate", "when": "vars.verdict_emitted or vars.review_stale"}
    ]


@pytest.mark.parametrize("state", [None, {"is_closed": False, "current_stage": None}])
def test_trajectory_monitor_tolerates_missing_nested_task_state(state: object) -> None:
    review = _step(_agent(), "review")
    get_task_handler = next(
        item
        for item in review["on_mcp_success"]
        if item["server"] == "gobby-tasks" and item["tool"] == "get_task"
    )
    evaluator = SafeExpressionEvaluator(
        {
            "tool_output": {
                "success": True,
                "result": {"state": state},
            }
        },
        {"bool": bool},
    )

    assert evaluator.evaluate(get_task_handler["when"]) is True
