"""Wiring tests for plan-adversary.yaml manifest-emission contract (§2.22.1, §2.22.3).

The plan-adversary agent must emit the ``## M1 Task Manifest`` YAML at the end
of the plan file before calling ``mark_task_review_approved``. The act of
writing the manifest forces the adversary to confront ambiguity it might
otherwise wave through.

These tests lock in:
  - the M1 heading ID (required by the canonical heading regex § 2.21),
  - the order: emit manifest BEFORE review approval,
  - Edit/Write are documented as permitted tools for the review step,
  - instructions scope writes to the plan file path only (§ 2.22.3).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents._yaml_helpers import find_step

pytestmark = pytest.mark.unit

ADVERSARY_PATH = Path("src/gobby/install/shared/workflows/agents/plan-adversary.yaml")


@pytest.fixture(scope="module")
def agent() -> AgentDefinitionBody:
    with ADVERSARY_PATH.open() as f:
        data = yaml.safe_load(f)
    return AgentDefinitionBody.model_validate(data)


class TestManifestEmissionOnApproval:
    """§2.22.1 — manifest emitted before mark_task_review_approved on clean review."""

    def test_instructions_describe_manifest_emission_on_approval(
        self, agent: AgentDefinitionBody
    ) -> None:
        instructions = agent.instructions or ""
        assert "Task Manifest" in instructions
        assert "mark_task_review_approved" in instructions

    def test_instructions_reference_M1_heading_id(self, agent: AgentDefinitionBody) -> None:
        """The canonical heading regex (§2.21) requires M1 as section ID."""
        instructions = agent.instructions or ""
        assert "## M1 Task Manifest" in instructions

    def test_manifest_emission_precedes_review_approval(self, agent: AgentDefinitionBody) -> None:
        """Manifest write must come BEFORE the approval call in instruction order."""
        instructions = agent.instructions or ""
        manifest_index = instructions.find("M1 Task Manifest")
        approval_index = instructions.find("mark_task_review_approved")
        assert manifest_index >= 0
        assert approval_index >= 0
        assert manifest_index < approval_index, (
            "manifest emission must be described before review approval in instructions"
        )

    def test_review_step_does_not_block_review_approval(self, agent: AgentDefinitionBody) -> None:
        """Sanity: the existing review approval wiring stays intact."""
        review = find_step(agent.steps or [], "review")
        assert review is not None
        blocked = review.blocked_mcp_tools or []
        assert "gobby-tasks:mark_task_review_approved" not in blocked


class TestScopedEditWriteSurface:
    """§2.22.3 — Edit/Write permission is documented and scoped to plan file path."""

    def test_review_step_permits_edit_and_write(self, agent: AgentDefinitionBody) -> None:
        """Edit and Write must be available so the adversary can append the manifest."""
        review = find_step(agent.steps or [], "review")
        assert review is not None
        allowed = review.allowed_tools
        if allowed == "all":
            return
        assert isinstance(allowed, list)
        assert "Edit" in allowed
        assert "Write" in allowed

    def test_instructions_scope_writes_to_plan_file_only(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "plan_file_path" in instructions or "plan file path" in instructions
        assert "Edit" in instructions and "Write" in instructions

    def test_instructions_forbid_writes_outside_plan_file(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        lowered = instructions.lower()
        assert "only" in lowered and ("plan file" in lowered or "plan_file_path" in lowered)
