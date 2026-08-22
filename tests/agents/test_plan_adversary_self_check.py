"""Wiring tests for plan-adversary manifest handoff and fallback (§2.22.4).

The adversary validates full typed manifest entries without writing the plan.
The coordinator performs the authoritative expansion parse through
``apply_plan_review_manifest``. Entry repair is capped at three retries.
"""

from __future__ import annotations

import re
from importlib.resources import files

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

ADVERSARY_PATH = files("gobby").joinpath("install/shared/workflows/agents/plan-adversary.yaml")


@pytest.fixture(scope="module")
def agent() -> AgentDefinitionBody:
    with ADVERSARY_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return AgentDefinitionBody.model_validate(data)


class TestSelfCheckGate:
    """Authoritative render and expansion parsing belong to compare-and-apply."""

    def test_self_check_delegates_to_manifest_apply(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.prompts.agent or ""
        assert "apply_plan_review_manifest" in instructions
        assert "authoritative" in instructions
        assert "compare-and-apply expansion parse" in instructions
        assert "Never edit the plan file" in instructions

    def test_pre_verdict_parsing_delegated_upstream(self, agent: AgentDefinitionBody) -> None:
        """Adversary does NOT re-parse pre-verdict — that's the planner-side
        ``validate_plan_file`` gate, run before every adversary spawn (§2.21.3).
        """
        instructions = agent.prompts.agent or ""
        assert "validate_plan_file" in instructions
        assert "Do NOT re-run the parser pre-verdict" in instructions


class TestRetryAndCap:
    def test_retry_capped_at_three(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.prompts.agent or ""
        assert re.search(
            r"\b3\s+retr(?:y|ies)\b|retr(?:y|ies)\D{0,15}\b3\b",
            instructions,
            re.IGNORECASE,
        ), "expected explicit '3 retries' cap in adversary instructions"


class TestNonYoloEscalates:
    def test_non_yolo_escalates_with_needs_human_prefix(self, agent: AgentDefinitionBody) -> None:
        """After cap exhausted, non-yolo calls escalate_task with the
        documented manifest-emission-failure prefix."""
        instructions = agent.prompts.agent or ""
        assert "escalate_task" in instructions
        assert "needs_human:manifest_emission_failure" in instructions


class TestYoloFallback:
    """yolo NEVER calls escalate_task on this path (top-level invariant)."""

    def test_yolo_never_escalates_after_cap(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.prompts.agent or ""
        lowered = instructions.lower()
        # Contract: "- yolo: do NOT call `escalate_task` (top-level yolo invariant"
        expected = "yolo: do not call `escalate_task` (top-level yolo invariant"
        assert expected in lowered

    def test_yolo_uses_typed_fallback_entries(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.prompts.agent or ""
        assert "deterministic full typed fallback entries" in instructions
        assert "emit_stub_manifest" not in instructions

    def test_yolo_keeps_audit_in_approval_notes(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.prompts.agent or ""
        assert "audit marker in approval notes" in instructions
        assert "Never write an audit section" in instructions

    def test_force_approve_is_payload_only(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.prompts.agent or ""
        assert "force-approve" in instructions or "force_approve" in instructions
        assert "result payload" in instructions
        assert "Never write" in instructions
