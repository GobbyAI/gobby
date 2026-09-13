"""Content tests for expansion-side Plan-Coverage Contract docs."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

EXPAND_SKILL = Path("src/gobby/install/shared/skills/gobby/references/plan/expansion.md")


def test_expand_skill_documents_coverage_contract() -> None:
    body = EXPAND_SKILL.read_text(encoding="utf-8")
    required = (
        "run_expansion_qa_coverage",
        "save_expansion_qa_result/check_expansion_qa_result",
        "completed run with failed coverage is unfinished",
        "one source section/manifest entry/leaf",
    )
    for term in required:
        assert term in body

    coverage = (EXPAND_SKILL.parent / "coverage.md").read_text()
    assert "covers:<plan-id>:<section-id>:<item-id>" in coverage
    assert "Free-form plan-ref labels do not count" in coverage
    assert "docs/contracts/plan-coverage.md" in coverage
