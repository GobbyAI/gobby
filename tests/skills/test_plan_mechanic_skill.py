"""Content-level tests for the bundled plan-mechanic methodology skill.

plan-mechanic is the bounded mechanical-repair step of the plan loop: it runs
between the planner's semantic repair and the next adversary round, fixes only
what `gobby plans validate` can detect, and stops on anything that needs a
design choice. These tests guard content drift: the skill stays mechanical
(never redesigns, never touches V1 fences or the manifest), covers every
validator lint code, requires project-aware validation in both modes, and
reports `needs-planner` notes instead of guessing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.skills.parser import parse_skill_file
from tests.skills.scenario_runner import run_recorded_skill_scenario

pytestmark = pytest.mark.unit

SKILL_PATH = Path("src/gobby/install/shared/skills/plan-mechanic/SKILL.md")
SCENARIO = Path(__file__).resolve().parent / "scenarios" / "plan-mechanic" / "bounded-repair.yaml"

LINT_CODES = (
    "target-coverage",
    "shared-target-ordering",
    "production-size-growth",
    "derived-carriers",
    "unresolved-dependency",
    "table-row-decomposition",
)


class TestPlanMechanicFrontmatter:
    def test_skill_parses_as_internal_methodology(self) -> None:
        parsed = parse_skill_file(SKILL_PATH)
        assert parsed.name == "plan-mechanic"
        assert "validate" in parsed.description.lower()
        assert parsed.is_internal()

    def test_audience_is_all(self) -> None:
        """Loaded by a spawned repair agent and by the interactive coordinator."""
        parsed = parse_skill_file(SKILL_PATH)
        assert parsed.audience_config is not None
        assert parsed.audience_config.audience == "all"


class TestPlanMechanicContent:
    @pytest.fixture
    def body(self) -> str:
        return SKILL_PATH.read_text(encoding="utf-8")

    def test_loads_restraint_and_plan_draft_first(self, body: str) -> None:
        assert 'get_skill(name="restraint")' in body
        assert 'get_skill(name="plan-draft")' in body

    def test_hard_boundaries_forbid_redesign_fences_manifest(self, body: str) -> None:
        lowered = body.lower()
        assert "never redesign" in lowered
        assert "never edit the `## v1 plan changelog`" in lowered
        assert "byte-identical" in lowered
        assert "never write the `## m1 task manifest`" in lowered

    def test_design_choice_stops_with_needs_planner(self, body: str) -> None:
        assert "needs-planner" in body
        assert "a design choice is a stop, not a guess" in body.lower()

    @pytest.mark.parametrize("code", LINT_CODES)
    def test_repair_table_covers_every_validator_lint(self, body: str, code: str) -> None:
        assert f"`{code}`" in body

    def test_repair_table_covers_symbol_target_forms(self, body: str) -> None:
        assert "mix exact symbols with `::*`" in body
        assert "scope-reason" in body
        assert 'gcode search-symbol "<name>" <path>' in body
        assert "followed by a blank line" in body

    def test_procedure_validates_both_modes_with_project_root(self, body: str) -> None:
        assert "uv run gobby plans validate <plan-file> -p <project-root>" in body
        assert "uv run gobby plans validate <plan-file> -p <project-root> --mode expansion" in body
        assert "`-p` is required" in body

    def test_procedure_is_bounded_and_checks_v1_diff(self, body: str) -> None:
        lowered = body.lower()
        assert "after five full passes" in lowered
        assert "git diff -- <plan-file>" in body

    def test_report_schema_fields(self, body: str) -> None:
        for field in ("validation:", "repairs:", "needs_planner:", "v1_changelog:", "ledger:"):
            assert field in body

    def test_exit_sends_message_then_ends_run(self, body: str) -> None:
        assert "`send_message`" in body
        assert "`end_agent_run`" in body


@pytest.mark.skill_tdd
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
