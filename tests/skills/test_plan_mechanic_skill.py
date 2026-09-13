"""Mechanical repair remains bounded and never becomes redesign."""

from pathlib import Path

import pytest

from gobby.skills.capability_catalog import load_capability_catalog
from tests.skills.scenario_runner import run_recorded_skill_scenario

pytestmark = pytest.mark.unit
REFERENCE = Path("src/gobby/install/shared/skills/gobby/references/plan/repair.md")
SCENARIO = Path(__file__).resolve().parent / "scenarios/plan-mechanic/bounded-repair.yaml"


@pytest.fixture
def body() -> str:
    return " ".join(REFERENCE.read_text().split())


def test_mechanic_catalog_identity() -> None:
    assert load_capability_catalog().folded_skills["plan-mechanic"] == (
        "gobby:references/plan/repair.md"
    )


def test_prerequisites_and_scope(body: str) -> None:
    for term in (
        "Load standalone `restraint`",
        "[drafting](drafting.md)",
        "[coverage](coverage.md)",
        "Read the linked coverage contract",
        "Run standard validation with project context",
        "expansion mode too only for a manifest-bearing artifact",
        "fix shape already determined by the narrative",
        "Sweep the whole plan",
        "only a split boundary/file already specified",
    ):
        assert term in body


def test_repair_preserves_decisions_and_immutable_checkpoints(body: str) -> None:
    for term in (
        "Never invent scope, new promises, ownership or a design choice",
        "return needs-planner with section, lint and exact missing decision",
        "Never edit M1, locked decisions or V1 checkpoints",
        "after five full passes",
        "before/after counts",
        "unrun-no-manifest",
        "byte-identical V1",
        "whether resealing is required",
    ):
        assert term in body


def test_evidence_recovery_preserves_transaction_order(body: str) -> None:
    for term in (
        "append canonical result, finalize, then apply accepted typed repairs",
        "invalid_repair: the atomic apply leaves bytes unchanged",
        "Idempotent repairs report already_present",
        "drift revokes the intent",
        "pending_lesson_mint",
        "never forge source hashes",
        "never launch attempts or expired evidence",
    ):
        assert term in body


def test_plan_mechanic_applies_bounded_repairs_instead_of_rewriting() -> None:
    result = run_recorded_skill_scenario(SCENARIO)

    assert result.baseline.action_names == ("rewrite_section", "respond")
    assert result.loaded.action_names == (
        "run_plan_validate",
        "apply_bounded_repair",
        "apply_bounded_repair",
        "run_plan_validate",
        "respond",
    )
    assert result.has_behavioral_delta
