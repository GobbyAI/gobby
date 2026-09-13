"""Recorded research-handoff scenarios and their required instruction entry points."""

from pathlib import Path

import pytest

from gobby.plans.parser import parse_plan
from gobby.plans.semantic_lint import lint_plan_document
from gobby.plans.symbol_targets import parse_symbol_targets
from gobby.tasks.expansion._contract import _build_contract_entry_work_task
from tests.skills.scenario_runner import run_recorded_skill_scenario

pytestmark = [pytest.mark.unit, pytest.mark.skill_tdd]

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "src/gobby/install/shared/skills"


@pytest.mark.parametrize(
    ("scenario", "guidance", "required"),
    [
        (
            "plan-draft/preserve-research.yaml",
            "gobby/references/plan/drafting.md",
            "its own Research context",
        ),
        (
            "development-discipline/research-unchanged.yaml",
            "gobby/references/development/obligations.md",
            "supplied research and specification",
        ),
        (
            "development-discipline/research-moved-symbol.yaml",
            "gobby/references/development/obligations.md",
            "Rediscover only stale or missing evidence",
        ),
    ],
)
def test_research_handoff_scenario(scenario: str, guidance: str, required: str) -> None:
    result = run_recorded_skill_scenario(Path(__file__).parent / "scenarios" / scenario)

    assert result.has_behavioral_delta
    assert required in (SKILLS / guidance).read_text(encoding="utf-8")


def test_example_preserves_research_in_leaf() -> None:
    example = Path(__file__).parent / "scenarios/plan-draft/research-context-example.md"
    plan = parse_plan(example, parse_mode="expansion")
    targets, issues = parse_symbol_targets(plan)
    assert issues == ()
    assert [target.reference for target in targets] == [
        "src/gobby/tasks/expansion/_contract.py::_build_contract_entry_work_task",
        "tests/skills/test_plan_research_context.py::test_example_preserves_research_in_leaf",
    ]
    assert lint_plan_document(plan, project_root=ROOT).issues == ()

    section = next(section for section in plan.sections if section.section_id == "1.1")
    leaf = _build_contract_entry_work_task(
        None,
        plan_doc=plan,
        entry=plan.manifest_entries[0],
        section=section,
        phase_id="phase-p1",
    )
    research = example.read_text(encoding="utf-8").split("**Research context:**", 1)[1]
    research = "**Research context:**" + research.split("**Acceptance:**", 1)[0].rstrip()
    assert research in leaf["description"]
