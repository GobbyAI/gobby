"""Contracts for provider-neutral parallel plan-review agents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

pytestmark = pytest.mark.unit

AGENTS_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/agents"


def _agent(name: str) -> dict[str, Any]:
    payload = yaml.safe_load((AGENTS_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _evaluator(
    *, tool_input: dict[str, object], variables: dict[str, object]
) -> SafeExpressionEvaluator:
    return SafeExpressionEvaluator(
        context={"tool_input": tool_input, "vars": variables, "tool_output": {}},
        allowed_funcs={"len": len, "str": str, "bool": bool},
    )


def test_adversary_agents_pin_the_reviewer_model() -> None:
    """The agent definition owns the reviewer model, not the coordinator skill."""
    for name in ("plan-adversary", "plan-adversary-taskless"):
        agent = _agent(name)
        assert agent["provider"] == "codex"
        assert agent["model"] == "gpt-5.6-sol"
        assert agent["reasoning_effort"] == "xhigh"
        assert agent["reasoning_required"] is False
        assert agent["isolation"] == "none"


def test_researcher_worker_inherits_the_adversary_model() -> None:
    """Lane workers follow whichever model the adversary resolved to."""
    worker = _agent("plan-review-researcher-taskless")
    assert worker["provider"] == "inherit"
    assert "model" not in worker
    assert worker["reasoning_effort"] == "high"
    assert worker["reasoning_required"] is False
    assert worker["isolation"] == "none"


def test_researcher_is_taskless_read_only_and_cannot_finalize_or_spawn() -> None:
    worker = _agent("plan-review-researcher-taskless")
    assert set(worker["blocked_tools"]) >= {"Edit", "Write", "NotebookEdit", "Task"}
    blocked = set(worker["blocked_mcp_tools"])
    assert "gobby-agents:spawn_agent" in blocked
    assert "gobby-tasks:create_task" in blocked
    assert "gobby-tasks:claim_task" in blocked
    assert "gobby-tasks:update_task" in blocked
    assert "gobby-tasks-ops:approve_review" in blocked
    assert "gobby-tasks-ops:reject_review" in blocked
    assert "gobby-plans:apply_plan_review_manifest" in blocked
    assert "gobby-plans:finalize_plan_review_evidence" in blocked
    assert "assigned_task_id" not in yaml.safe_dump(worker)
    assert "candidate_issues" in worker["instructions"]
    assert "Do not emit findings" in worker["instructions"]


def test_both_adversaries_limit_fanout_to_researcher_slug_and_three_runs() -> None:
    for name in ("plan-adversary", "plan-adversary-taskless"):
        adversary = _agent(name)
        review = next(step for step in adversary["steps"] if step["name"] == "review")
        rules = review["on_mcp_before"]
        slug_rule = next(rule for rule in rules if "tool_input.get('agent')" in rule["when"])
        lane_rule = next(rule for rule in rules if "review_worker_lanes" in rule["when"])
        cap_rule = next(rule for rule in rules if "review_worker_run_ids" in rule["when"])
        assert "plan-review-researcher-taskless" in slug_rule["when"]
        assert "task_id" in slug_rule["when"]
        assert "isolation" in slug_rule["when"]
        for lane_id in (
            "requirements_traceability",
            "repository_blast_radius",
            "runtime_invariants",
        ):
            assert f"review_lane: {lane_id}" in lane_rule["when"]
            assert lane_id in lane_rule["when"]
        assert ">= 3" in cap_rule["when"]
        assert adversary["step_variables"]["review_worker_lanes"] == []
        assert "gobby-agents:spawn_agent" not in set(review["blocked_mcp_tools"])


def test_lane_fanout_hook_rejects_missing_multiple_and_duplicate_markers() -> None:
    review = next(step for step in _agent("plan-adversary")["steps"] if step["name"] == "review")
    lane_rule = next(
        rule for rule in review["on_mcp_before"] if "review_worker_lanes" in rule["when"]
    )
    base_input: dict[str, object] = {
        "agent": "plan-review-researcher-taskless",
        "isolation": "none",
    }
    valid_input = {**base_input, "prompt": "review_lane: requirements_traceability"}
    assert (
        _evaluator(tool_input=valid_input, variables={"review_worker_lanes": []}).evaluate(
            lane_rule["when"]
        )
        is False
    )
    assert _evaluator(
        tool_input={**base_input, "prompt": "no lane marker"},
        variables={"review_worker_lanes": []},
    ).evaluate(lane_rule["when"])
    assert _evaluator(
        tool_input={
            **base_input,
            "prompt": ("review_lane: requirements_traceability review_lane: runtime_invariants"),
        },
        variables={"review_worker_lanes": []},
    ).evaluate(lane_rule["when"])
    assert _evaluator(
        tool_input=valid_input,
        variables={"review_worker_lanes": ["requirements_traceability"]},
    ).evaluate(lane_rule["when"])


def test_lane_success_hook_records_the_canonical_marker() -> None:
    review = next(step for step in _agent("plan-adversary")["steps"] if step["name"] == "review")
    lane_capture = next(
        rule for rule in review["on_mcp_success"] if rule.get("variable") == "review_worker_lanes"
    )
    value = _evaluator(
        tool_input={"prompt": "review_lane: repository_blast_radius"},
        variables={"review_worker_lanes": ["requirements_traceability"]},
    ).evaluate_value(lane_capture["value"])
    assert value == ["requirements_traceability", "repository_blast_radius"]


def test_parent_contract_owns_dispositions_coverage_and_verdict() -> None:
    for name in ("plan-adversary", "plan-adversary-taskless"):
        instructions = _agent(name)["instructions"]
        assert "sole verdict" in instructions
        assert "derive_plan_review_manifest" in instructions
        assert "validate_plan_review_coverage" in instructions
        assert "coverage_attestation" in instructions
        assert "source drift" in instructions.lower()


def test_parent_contract_covers_complexity_and_worker_fallbacks() -> None:
    for name in ("plan-adversary", "plan-adversary-taskless"):
        instructions = " ".join(_agent(name)["instructions"].split()).lower()
        assert "sequential mode" in instructions
        assert "parallel mode" in instructions
        assert "at most one" in instructions
        assert "capacity shortage" in instructions
        assert "timeout" in instructions
        assert "malformed output" in instructions
        assert "worker failure" in instructions
        assert "timeout=900" in instructions
