"""Drafting references retain the narrative, grammar, and validation contracts."""

from pathlib import Path

import pytest

from gobby.skills.capability_catalog import load_capability_catalog

pytestmark = pytest.mark.unit
REFERENCES = Path("src/gobby/install/shared/skills/gobby/references/plan")
CONTRACT = Path("docs/contracts/plan-coverage.md")
GUIDE = Path("docs/guides/spec-writing.md")


def test_drafting_is_catalogued_with_required_grammar() -> None:
    catalog = load_capability_catalog()
    assert catalog.folded_skills["plan-draft"] == "gobby:references/plan/drafting.md"
    body = (REFERENCES / "drafting.md").read_text()
    assert "[coverage](coverage.md)" in body
    assert "docs/contracts/plan-coverage.md" in body


@pytest.mark.parametrize(
    "phrase",
    [
        "code",
        "config",
        "docs",
        "refactor",
        "test",
        "implementation_domain",
        "1:1",
        "additional_skills",
        "tdd:required",
        "not TDD-eligible",
        "expansion manifests reject them",
        "Filler tasks",
        "standalone test infrastructure",
    ],
)
def test_category_and_tdd_contract_remains_authoritative(phrase: str) -> None:
    assert phrase in CONTRACT.read_text()


def test_executable_config_tdd_remains_conditional() -> None:
    body = " ".join(GUIDE.read_text().split())
    assert "`config` | conditional" in body
    assert "executable behavior" in body
    assert "Do not add filler deliverables" in body


def test_draft_preserves_executor_context_and_atomicity() -> None:
    body = " ".join((REFERENCES / "drafting.md").read_text().split())
    for term in (
        "each section concrete Targets, its own Research context",
        "exact file-qualified symbols",
        "Copy shared findings into every executor's section",
        "Split independently implementable/testable/committable outcomes",
        "**Granularity:**",
        "more than six acceptance items",
        "two independently testable state machines/lifecycle owners",
        "no M1",
        "expansion validation is explicitly unrun",
        "both base and expansion validation",
        "scratch store",
        "complete latest Markdown draft",
        "Materialize the complete draft once writable",
        "sole authority",
        "never overwrite it with a stale mirror",
    ):
        assert term in body


def test_coverage_reference_preserves_target_and_heading_constraints() -> None:
    body = " ".join((REFERENCES / "coverage.md").read_text().split())
    for term in (
        "## P<N>: Name",
        "without a blank line after `Targets:`",
        "path::qualified_name",
        "path::*",
        "same-line scope-reason",
        "Bare paths are for new or zero-symbol files",
        "UUIDs, line numbers",
        "mixed exact/wildcard scopes are invalid",
        "Sweep usages and literal consumers",
        "including tests",
        "required derived carrier",
        "Shared Target paths require dependency",
        "one acceptance item per data row",
        "missing-index or skipped check",
    ):
        assert term in body
    assert "requirement-source" not in body
    assert "needs_requirements" not in body
