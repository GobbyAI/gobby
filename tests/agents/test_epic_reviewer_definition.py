"""Phase 2 contract tests for the epic-reviewer agent definition."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

pytestmark = pytest.mark.unit


def _agent() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/epic-reviewer.yaml"
    )
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def test_three_outcomes() -> None:
    agent = _agent()
    review_step = next(step for step in agent["step_workflow"]["steps"] if step["name"] == "review")
    success_tools = {item["tool"] for item in review_step.get("on_mcp_success", [])}

    assert {
        "complete_stage",
        "fail_stage",
        "escalate_task",
    } <= success_tools


def test_review_step_stays_active_after_mcp_error() -> None:
    review_step = next(
        step for step in _agent()["step_workflow"]["steps"] if step["name"] == "review"
    )

    assert review_step["mcp_error_policy"] == "stay"


def test_success_path_uses_complete_stage_for_in_progress_epic_qa() -> None:
    agent = _agent()
    review_step = next(step for step in agent["step_workflow"]["steps"] if step["name"] == "review")
    blocked = set(review_step["blocked_mcp_tools"])
    instructions = agent["prompts"]["agent"]
    status = review_step["status_message"]

    assert "complete_stage" in instructions
    assert 'stage_name="epic_qa"' in instructions
    assert "validation_override_reason" in instructions
    assert "After successful final validation in REVIEW" in instructions
    assert "pending terminal-verdict obligation" in instructions
    assert "After successful final validation" in status
    assert 'complete_stage(stage_name="epic_qa"' in status
    assert "gobby-tasks-ops:approve_review" in blocked
    assert "gobby-tasks-ops:reject_review" in blocked
    assert "gobby-agents:end_agent_run" in blocked
    assert (
        "gobby-agents:end_agent_run"
        in next(step for step in agent["step_workflow"]["steps"] if step["name"] == "terminate")["allowed_mcp_tools"]
    )


def test_reads_subtree() -> None:
    agent = _agent()
    claim_step = next(step for step in agent["step_workflow"]["steps"] if step["name"] == "claim")
    review_text = next(step for step in agent["step_workflow"]["steps"] if step["name"] == "review")[
        "status_message"
    ]

    assert "gobby-tasks:get_task" in claim_step["allowed_mcp_tools"]
    assert "gobby-tasks:list_tasks" in claim_step["allowed_mcp_tools"]
    assert "list_tasks(parent_task_id=" in agent["prompts"]["agent"]
    assert "list_tasks(parent_task_id=assigned_task_id)" in review_text


def test_docs_epics_can_use_discovery_brief_plan_substitute() -> None:
    agent = _agent()
    instructions = agent["prompts"]["agent"]
    status = next(step for step in agent["step_workflow"]["steps"] if step["name"] == "review")["status_message"]

    assert "Discovery Brief" in instructions
    assert "descendant task set" in instructions
    assert "task references" in instructions
    assert "Discovery Brief" in status
    assert "plan substitute" in status


def test_epic_review_order_is_spec_quality_testing_proportionality() -> None:
    agent = _agent()
    instructions = agent["prompts"]["agent"]
    status = next(step for step in agent["step_workflow"]["steps"] if step["name"] == "review")["status_message"]

    # Anchor on the explicit "Review in order" sentence: the `proportionality`
    # skill name also appears earlier in the skill-load list, so a bare
    # str.index would resolve it before the review dimensions.
    order_anchor = instructions.index("Review in order")
    ordered = ["spec_compliance", "code_quality", "testing", "proportionality"]
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert instructions.index(earlier, order_anchor) < instructions.index(later, order_anchor)
    # The dimension is reframed from yagni onto the shared proportionality criterion.
    assert "yagni" not in instructions
    assert "operational_risk" not in instructions
    assert "aggregate diff" in status


def test_loads_required_skills_before_review() -> None:
    agent = _agent()
    steps = {step["name"]: step for step in agent["step_workflow"]["steps"]}
    load_step = steps["load_skill"]

    assert agent["step_workflow"]["variables"]["required_skills"] == [
        "code-index",
        "epic-review",
        "review-learning",
        "tech-writer",
        "tasks",
        "proportionality",
    ]
    assert load_step["allowed_mcp_tools"] == ["gobby-skills:get_skill"]
    for skill_name in agent["step_workflow"]["variables"]["required_skills"]:
        assert f'get_skill(name="{skill_name}")' in load_step["status_message"]
    assert load_step["transitions"] == [
        {
            "to": "closed_review",
            "when": (
                "vars.closed_epic and all(skill in vars.get('loaded_skills', []) "
                "for skill in vars.required_skills)"
            ),
        },
        {
            "to": "review",
            "when": (
                "not vars.closed_epic and all(skill in vars.get('loaded_skills', []) "
                "for skill in vars.required_skills)"
            ),
        },
    ]

    success = load_step["on_mcp_success"][0]
    assert success["server"] == "gobby-skills"
    assert success["tool"] == "get_skill"
    assert success["variable"] == "required_skills_loaded"


def test_closed_epic_routes_to_post_hoc_review_with_reopen_permission() -> None:
    agent = _agent()
    steps = {step["name"]: step for step in agent["step_workflow"]["steps"]}
    claim = steps["claim"]
    closed_review = steps["closed_review"]

    closed_detection = next(
        hook
        for hook in claim["on_mcp_success"]
        if hook["server"] == "gobby-tasks" and hook["tool"] == "get_task"
    )
    assert "state" in closed_detection["when"]
    assert "is_closed" in closed_detection["when"]
    assert closed_detection["variable"] == "closed_epic"
    assert claim["transitions"] == [
        {"to": "load_skill", "when": "vars.task_claimed or vars.closed_epic"}
    ]
    assert "gobby-tasks:reopen_task" not in closed_review["blocked_mcp_tools"]
    assert "gobby-tasks:close_task" in closed_review["blocked_mcp_tools"]
    assert "Call reopen_task only" in agent["prompts"]["agent"]


def test_tdd_audit_evidence_is_language_aware() -> None:
    agent = _agent()
    instructions = agent["prompts"]["agent"]
    review_text = next(step for step in agent["step_workflow"]["steps"] if step["name"] == "review")[
        "status_message"
    ]

    for text in (instructions, review_text):
        assert "supported-language" in text
        assert "test-quality audit" in text
        assert "missing baseline" in text.lower()
        assert "not a skip reason" in text
        assert "unsupported-language warning" in text
        assert "repo-native validation" in text
