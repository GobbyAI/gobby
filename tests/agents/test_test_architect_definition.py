"""Contracts for architect-owned test architecture."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


ROOT = Path(__file__).resolve().parents[2]


def test_test_architecture_skill_outputs_structured_prose_not_expansion_tasks() -> None:
    skill_path = ROOT / "src/gobby/install/shared/skills/architecture/SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")

    assert "## Test Architecture" in skill_text
    for heading in (
        "### Integration",
        "### E2E",
        "### Regression",
        "### Contract",
        "### Infrastructure",
    ):
        assert heading in skill_text
    assert "Do NOT write `### N.N` task sections" in skill_text
    assert "[category: test] leaves" in skill_text
