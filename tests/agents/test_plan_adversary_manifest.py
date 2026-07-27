"""Wiring tests for canonical plan-adversary manifest handoff."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents._yaml_helpers import find_step

pytestmark = pytest.mark.unit

ADVERSARY_PATH = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/workflows/agents/plan-adversary.yaml"
)


@pytest.fixture(scope="module")
def agent() -> AgentDefinitionBody:
    with ADVERSARY_PATH.open() as f:
        data = yaml.safe_load(f)
    return AgentDefinitionBody.model_validate(data)


class TestManifestEmissionOnApproval:
    """§2.22.1 — typed manifest handed off through approve_review."""

    def test_instructions_describe_typed_manifest_handoff_on_approval(
        self, agent: AgentDefinitionBody
    ) -> None:
        instructions = agent.instructions or ""
        assert "MANIFEST HANDOFF" in instructions
        assert "manifest_entries" in instructions
        assert "routing_decisions" in instructions
        assert "coverage_attestation" in instructions
        assert "approve_review" in instructions

    def test_instructions_require_canonical_round_result(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "canonical `round_result`" in instructions
        for field in (
            "`round_number`",
            "`findings`",
            "`manifest_entries`",
            "`routing_decisions`",
            "`coverage_attestation`",
            "`evidence_id`",
        ):
            assert field in instructions

    def test_manifest_emission_precedes_review_approval(self, agent: AgentDefinitionBody) -> None:
        """Typed manifest derivation must precede the approval call."""
        instructions = agent.instructions or ""
        manifest_index = instructions.find("Record routing decisions")
        derive_index = instructions.find("derive_plan_review_manifest", manifest_index)
        approval_index = instructions.find("On success, call `approve_review`")
        assert manifest_index >= 0
        assert derive_index >= 0
        assert approval_index >= 0
        assert manifest_index < derive_index < approval_index, (
            "manifest derivation must be described before review approval in instructions"
        )

    def test_invalid_plan_identity_rejects_before_manifest_derivation(
        self, agent: AgentDefinitionBody
    ) -> None:
        """Identity failure must stop before typed manifest derivation."""
        instructions = agent.instructions or ""
        normalized = " ".join(instructions.split())
        guard_index = normalized.find("If the Plan Identity Precondition fails")
        reject_index = normalized.find("Plan Identity Precondition failed")
        manifest_index = normalized.find("Record routing decisions")
        assert guard_index >= 0
        assert reject_index >= 0
        assert manifest_index >= 0
        assert reject_index < manifest_index
        identity_guard = normalized[guard_index:manifest_index]
        assert "reject_review" in identity_guard
        assert "Do NOT call `approve_review`" in identity_guard

    def test_raw_instruction_order_keeps_identity_guard_before_manifest_derivation(
        self, agent: AgentDefinitionBody
    ) -> None:
        instructions = agent.instructions or ""
        manifest_index = instructions.find("Record routing decisions")
        reject_index = instructions.find("If the Plan Identity Precondition fails")
        assert reject_index >= 0
        assert manifest_index >= 0
        assert reject_index < manifest_index
        identity_guard = instructions[reject_index:manifest_index]
        assert "reject_review" in identity_guard
        assert "Do NOT call `approve_review`" in identity_guard

    def test_review_step_does_not_block_review_approval(self, agent: AgentDefinitionBody) -> None:
        """Sanity: the existing review approval wiring stays intact."""
        review = find_step(agent.steps or [], "review")
        assert review is not None
        blocked = review.blocked_mcp_tools or []
        assert "gobby-tasks-ops:approve_review" not in blocked


class TestLessonBackfill:
    def test_failed_approval_mint_backfills_once_then_relays_remaining_failure(
        self,
        agent: AgentDefinitionBody,
    ) -> None:
        review = find_step(agent.steps or [], "review")
        backfill = find_step(agent.steps or [], "backfill_lessons")
        relay = find_step(agent.steps or [], "relay_backfill_failure")
        assert review is not None
        assert backfill is not None
        assert relay is not None

        assert any(
            transition.to == "backfill_lessons"
            and transition.when == "vars.lesson_backfill_required"
            for transition in review.transitions
        )
        assert backfill.allowed_mcp_tools == ["gobby-tasks-ops:backfill_plan_review_lessons"]
        assert any(
            transition.to == "relay_backfill_failure"
            and transition.when == "vars.lesson_backfill_failed"
            for transition in backfill.transitions
        )
        assert relay.allowed_mcp_tools == ["gobby-agents:send_message"]
        assert any(transition.to == "terminate" for transition in relay.transitions)


class TestCoordinatorOwnedWrites:
    """§2.22.3 — reviewer emits typed data; coordinator owns plan mutations."""

    def test_instructions_forbid_direct_plan_edits(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "Never edit the plan file" in instructions
        assert "Do NOT edit the plan file" in instructions

    def test_instructions_delegate_manifest_application(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "coordinator" in instructions
        assert "apply_plan_review_manifest" in instructions

    def test_review_step_blocks_coordinator_owned_plan_writes(
        self,
        agent: AgentDefinitionBody,
    ) -> None:
        review = find_step(agent.steps or [], "review")
        assert review is not None
        assert {
            "gobby-plans:apply_plan_review_manifest",
            "gobby-plans:finalize_plan_review_evidence",
            "gobby-plans:checkpoint_plan_review_lesson_mint",
        } <= set(review.blocked_mcp_tools or [])
