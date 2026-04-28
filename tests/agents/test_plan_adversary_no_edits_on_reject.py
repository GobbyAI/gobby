"""Wiring tests for plan-adversary.yaml — no plan-file edits on rejection (§2.22.5).

When emitting findings (rejection rounds), the adversary must NOT edit the
plan file. Plan edits between rounds are the planner's responsibility (§2.23).
The adversary writes only into the planning task's description (via
``mark_task_review_rejected``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody

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


def test_instructions_forbid_plan_edits_on_rejection(agent: AgentDefinitionBody) -> None:
    instructions = agent.instructions or ""
    assert (
        "Do NOT edit the plan file" in instructions
        or "do not edit the plan" in instructions.lower()
    )


def test_instructions_route_rejection_through_findings_only(
    agent: AgentDefinitionBody,
) -> None:
    """Rejections go through mark_task_review_rejected with rejection_notes;
    plan-file content stays untouched."""
    instructions = agent.instructions or ""
    assert "mark_task_review_rejected" in instructions
    assert "rejection_notes" in instructions


def test_instructions_attribute_plan_edits_to_planner(agent: AgentDefinitionBody) -> None:
    """Plan edits between rounds are the planner's responsibility (§2.23)."""
    instructions = agent.instructions or ""
    lowered = instructions.lower()
    assert "planner" in lowered
    assert "between rounds" in lowered or "next round" in lowered or "§2.23" in instructions
