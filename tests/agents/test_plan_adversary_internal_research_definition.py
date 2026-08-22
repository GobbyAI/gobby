"""Contracts for provider-native internal plan-adversary research."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "src/gobby/install/shared/workflows/agents"
MANIFEST_PATH = REPO_ROOT / "src/gobby/install/bundled_content_manifest.json"
REMOVED_RESEARCHER = "plan-review-researcher-taskless"
ADVERSARIES = ("plan-adversary", "plan-adversary-taskless")
PLAN_AGENTS = (*ADVERSARIES, "plan-enhancer", "plan-enhancer-taskless")
LANES = (
    "requirements_traceability",
    "repository_blast_radius",
    "runtime_invariants",
)


def _agent(name: str) -> dict[str, Any]:
    payload = yaml.safe_load((AGENTS_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _review(agent: dict[str, Any]) -> dict[str, Any]:
    return next(step for step in agent["step_workflow"]["steps"] if step["name"] == "review")


def test_adversary_agents_pin_the_reviewer_model() -> None:
    """The agent definition owns the reviewer model, not the coordinator skill."""
    for name in ADVERSARIES:
        agent = _agent(name)
        assert agent["provider"] == "codex"
        assert agent["model"] == "gpt-5.6-sol"
        assert agent["reasoning_effort"] == "xhigh"
        assert agent["reasoning_required"] is False
        assert agent["isolation"] == "none"


def test_adversaries_have_no_experiment_timeout_contract() -> None:
    for name in ADVERSARIES:
        agent = _agent(name)
        instructions = " ".join(agent["prompts"]["agent"].split())

        assert "timeout" not in agent
        assert "2700-second agent timeout" not in instructions


def test_adversaries_read_one_complete_evidence_snapshot() -> None:
    for name in ADVERSARIES:
        instructions = " ".join(_agent(name)["prompts"]["agent"].split())

        assert "one complete decoded immutable snapshot" in instructions
        assert "`get_plan_review_snapshot" in instructions
        assert "offset: 0" not in instructions
        assert "next_offset" not in instructions
        assert "three lane results" in instructions
        assert "candidate dispositions" in instructions
        assert "shadow-manifest status" in instructions


def test_removed_researcher_is_absent_from_inventory_and_manifest() -> None:
    assert not (AGENTS_DIR / f"{REMOVED_RESEARCHER}.yaml").exists()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert f"workflows/agents/{REMOVED_RESEARCHER}.yaml" not in manifest["files"]


def test_all_plan_agents_block_gobby_spawn_at_agent_level() -> None:
    for name in PLAN_AGENTS:
        agent = _agent(name)
        assert "gobby-agents:spawn_agent" in set(agent["blocked_mcp_tools"])
        for step in agent["step_workflow"]["steps"]:
            allowed = set(step.get("allowed_mcp_tools") or [])
            assert "gobby-agents:spawn_agent" not in allowed


def test_adversaries_use_internal_three_lane_research_contract() -> None:
    for name in ADVERSARIES:
        agent = _agent(name)
        instructions = agent["prompts"]["agent"]
        normalized = " ".join(instructions.split()).lower()

        assert "read-only provider-native internal subagent" in normalized
        assert "current cli/runtime's internal collaboration facility" in normalized
        assert "run the three lanes concurrently" in normalized
        assert "at most one internal subagent per lane" in normalized
        for lane in LANES:
            assert lane in instructions
        for field in (
            "lane_id",
            "status: completed",
            "section_ids_checked",
            "source_citations",
            "candidate_issues",
            "candidate_id",
            "section_ids",
            "violated_invariant",
            "suggested_fix",
            "adjacent_sites_checked",
        ):
            assert field in instructions
        for fallback in (
            "capacity shortage",
            "unavailable internal collaboration",
            "subagent failure",
            "malformed output",
            "sequential",
        ):
            assert fallback in normalized


def test_parent_adversary_retains_evidence_and_verdict_ownership() -> None:
    for name in ADVERSARIES:
        instructions = " ".join(_agent(name)["prompts"]["agent"].split()).lower()
        assert "sole verdict" in instructions
        assert "derive_plan_review_manifest" in instructions
        assert "validate_plan_review_coverage" in instructions
        assert "coverage_attestation" in instructions
        assert "candidate" in instructions
        assert "emitted_finding" in instructions
        assert "dismissed" in instructions
        assert "lane results" in instructions
        assert "candidate dispositions" in instructions
        assert "shadow-manifest status" in instructions
        assert "cross-lane interaction" in instructions
        assert "adjacent-variant" in instructions


def test_taskless_review_status_allows_protocol_failure_without_verdict() -> None:
    status = _review(_agent("plan-adversary-taskless"))["status_message"]
    normalized = " ".join(status.split())

    assert "When validate_plan_review_coverage succeeds" in normalized
    assert "omit verdict" in normalized
    assert "protocol_failure" in normalized
    assert "exact tool error and draft findings" in normalized


def test_adversaries_remove_gobby_worker_state_and_spawn_hooks() -> None:
    forbidden_tools = {
        "gobby-agents:can_spawn_agent",
        "gobby-agents:wait_for_agent",
        "gobby-agents:get_agent_result",
    }
    for name in ADVERSARIES:
        agent = _agent(name)
        raw = yaml.safe_dump(agent)
        review = _review(agent)

        assert REMOVED_RESEARCHER not in raw
        assert "review_worker_run_ids" not in raw
        assert "review_worker_lanes" not in raw
        assert forbidden_tools.isdisjoint(review.get("allowed_mcp_tools") or [])
        for hook_name in ("on_mcp_before", "on_mcp_success", "on_mcp_error"):
            for handler in review.get(hook_name) or []:
                assert (handler.get("server"), handler.get("tool")) != (
                    "gobby-agents",
                    "spawn_agent",
                )


def test_enhancers_remain_single_agent_better_bigger_advisers() -> None:
    for name in ("plan-enhancer", "plan-enhancer-taskless"):
        agent = _agent(name)
        instructions = agent["prompts"]["agent"]
        assert "Better" in instructions
        assert "Bigger" in instructions
        assert "Do NOT spawn other agents" in instructions
        assert "gobby-agents:spawn_agent" in set(agent["blocked_mcp_tools"])
