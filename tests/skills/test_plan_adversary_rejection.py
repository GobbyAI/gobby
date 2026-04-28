"""Plan-adversary Plan-Coverage Contract rejection content tests."""

from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLAN_REVIEW = PROJECT_ROOT / "src/gobby/install/shared/skills/plan-review/SKILL.md"
ADVERSARY = PROJECT_ROOT / "src/gobby/install/shared/workflows/agents/plan-adversary.yaml"

MALFORMED_CASES = [
    (
        "missing ID",
        "## This heading has no section ID\n`kind: framing`\n",
        ("missing ID", "canonical regex"),
    ),
    (
        "missing kind",
        "## A1 Missing Kind\n\n**Acceptance:**\n\n- A1.1 - Done. file: `x.py`.\n",
        ("missing kind", "kind:"),
    ),
    (
        "missing acceptance",
        "## A1 Missing Acceptance\n`kind: deliverable`\n",
        ("missing acceptance", "**Acceptance:**"),
    ),
    (
        "ID collision",
        "## A1 First\n`kind: framing`\n\n## A1 Duplicate\n`kind: framing`\n",
        ("ID collision", "duplicate section ID"),
    ),
    (
        "malformed item ID",
        "## A1 Bad Item\n`kind: deliverable`\n\n**Acceptance:**\n\n- B1.1 - Done. file: `x.py`.\n",
        ("malformed item ID", "dotted-prefix-match"),
    ),
    (
        "malformed deferral",
        "## A1 Deferred\n`kind: deferred`\n\ndeferral:\n  task_ref: '#1'\n",
        ("malformed deferral", "task_ref", "original_acceptance_items"),
    ),
    (
        "zero artifact references",
        "## A1 No Artifact\n`kind: deliverable`\n\n**Acceptance:**\n\n- A1.1 - Done.\n",
        ("zero artifact references", "file:", "symbol:", "test:", "behavior:"),
    ),
    (
        "table-row decomposition",
        (
            "## A1 Table\n`kind: deliverable`\n\n"
            "| Work |\n| --- |\n| one |\n| two |\n| three |\n\n"
            "**Acceptance:**\n\n- A1.1 - Done. file: `x.py`.\n"
        ),
        ("table-row decomposition", "table data-row count", "missing rows"),
    ),
]


def _plan_review_body() -> str:
    return PLAN_REVIEW.read_text(encoding="utf-8")


def _adversary_prompt() -> str:
    data = yaml.safe_load(ADVERSARY.read_text(encoding="utf-8"))
    agent = AgentDefinitionBody.model_validate(data)
    return agent.build_prompt_preamble() or ""


@pytest.mark.parametrize(("cause", "malformed_plan", "required_terms"), MALFORMED_CASES)
def test_rejects_each_case(
    cause: str,
    malformed_plan: str,
    required_terms: tuple[str, ...],
) -> None:
    assert malformed_plan
    body = _plan_review_body()
    assert f"Plan-Coverage Contract rejection: {cause}" in body
    for term in required_terms:
        assert term in body


def test_rejects_table_row_decomposition_violation() -> None:
    five_row_fixture = (
        "## A7.4 Table Work\n"
        "`kind: deliverable`\n\n"
        "| Row | Work |\n"
        "| --- | --- |\n"
        "| 1 | parser |\n"
        "| 2 | manifest |\n"
        "| 3 | cli |\n"
        "| 4 | evidence |\n"
        "| 5 | docs |\n\n"
        "**Acceptance:**\n\n"
        "- A7.4.1 - Parser. file: `src/parser.py`.\n"
        "- A7.4.2 - Manifest. file: `src/manifest.py`.\n"
    )
    assert five_row_fixture.count("\n|") >= 7

    body = _plan_review_body()
    lowered = body.lower()
    assert "table-row decomposition" in lowered
    assert "missing rows" in lowered
    assert "table data-row count" in body


def test_plan_adversary_prompt_wires_parser_callable() -> None:
    prompt = _adversary_prompt()
    assert "gobby.plans.parser.parse_plan" in prompt
    assert "PlanParseError" in prompt
    assert "plan-review" in prompt
