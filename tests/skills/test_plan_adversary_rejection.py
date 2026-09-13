"""Plan-adversary Plan-Coverage Contract rejection content tests."""

from pathlib import Path

import pytest
import yaml

from gobby.plans.review_evidence_io import parse_plan_bytes
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.semantic_lint import lint_plan_document
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLAN_REVIEW = PROJECT_ROOT / "src/gobby/install/shared/skills/gobby/references/plan/review.md"
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
    references = PLAN_REVIEW.parent / "references"
    return "\n\n".join(
        [
            PLAN_REVIEW.read_text(encoding="utf-8"),
            *(reference.read_text() for reference in sorted(references.glob("*.md"))),
        ]
    )


def _adversary_prompt() -> str:
    data = yaml.safe_load(ADVERSARY.read_text(encoding="utf-8"))
    agent = AgentDefinitionBody.model_validate(data)
    return agent.prompt_for("agent") or ""


@pytest.mark.parametrize(("cause", "malformed_plan", "required_terms"), MALFORMED_CASES)
def test_grammar_gate_and_review_boundary(
    cause: str,
    malformed_plan: str,
    required_terms: tuple[str, ...],
) -> None:
    # Exercise the upstream grammar/semantic gate instead of pinning retired prose.
    content = ("# Rejection fixture\n**Plan ID:** rejection\n\n" + malformed_plan).encode()
    try:
        document = parse_plan_bytes("rejection.md", content)
    except ReviewEvidenceError as exc:
        assert exc.code == "invalid_plan"
        expected = {
            "missing kind": "missing kind: front-matter",
            "missing acceptance": "missing **Acceptance:** block",
            "ID collision": "duplicate section ID",
            "malformed item ID": "does not belong to section",
            "malformed deferral": "missing YAML deferral object",
            "zero artifact references": "has no artifact reference",
        }
        assert expected[cause] in str(exc)
    else:
        if cause == "missing ID":
            # Unnumbered framing is outside the typed section inventory; the
            # qualitative reviewer must still trace every requirement.
            assert document.sections == ()
            assert "Trace each obligation to acceptance" in _plan_review_body()
            return
        issues = lint_plan_document(document).issues
        assert any(issue.code == "table-row-decomposition" for issue in issues), required_terms


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
    assert "[coverage](coverage.md)" in lowered
    document = parse_plan_bytes("rows.md", five_row_fixture.encode())
    assert any(
        issue.code == "table-row-decomposition" for issue in lint_plan_document(document).issues
    )


def test_accepts_standalone_category_test_deliverables() -> None:
    contract = (PROJECT_ROOT / "docs/contracts/plan-coverage.md").read_text()
    assert "Filler tasks" in contract
    assert "`category: test` is valid for standalone test infrastructure" in contract
    assert "parity" in contract
    assert "own acceptance criteria" in contract


def test_plan_adversary_prompt_documents_upstream_draft_validation_contract() -> None:
    prompt = " ".join(_adversary_prompt().split())
    assert "Mechanical parser rejection happens upstream." in prompt
    assert "uv run gobby plans validate <plan-file>" in prompt
    assert "spawn gate runs the same internal validator" in prompt
    assert "typed grammar has already passed the draft-mode contract gate" in prompt


def test_review_methodology_delegates_mechanical_blast_radius_verification() -> None:
    body = " ".join(_plan_review_body().split())

    assert "runtime_invariants" in body
    assert "repository_blast_radius" in body
    assert "spot-checking" in body
    assert "deterministic report" in body
    assert "delegated-verified" in body


def test_review_methodology_does_not_confuse_mechanical_and_semantic_success() -> None:
    body = " ".join(_plan_review_body().split())
    assert "Base-validate canonical bytes immediately before each round" in body
    assert "Walk requirements, all inputs/control-flow branches" in body
    assert "Do not approve a plan you do not understand" in body
