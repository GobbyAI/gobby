"""Content tests for expansion-side Plan-Coverage Contract docs."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

EXPAND_SKILL = Path("src/gobby/install/shared/skills/expand/SKILL.md")


def test_expand_skill_documents_coverage_contract() -> None:
    body = EXPAND_SKILL.read_text(encoding="utf-8")
    required = (
        ".coverage-ledger.yaml",
        "covers:<plan-id>:<section-id>:<item-id>",
        "expansion-qa",
        "plan-ref:",
        "not honored",
        "docs/contracts/plan-coverage.md",
    )
    for term in required:
        assert term in body
