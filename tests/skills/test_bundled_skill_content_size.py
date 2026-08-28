"""Authoring ceiling checks for Gobby-owned bundled skill instructions."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.config.skills import SkillsConfig
from gobby.skills.authoring import find_bundled_content_violations

pytestmark = [pytest.mark.unit, pytest.mark.skill_tdd]

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_SKILLS = REPO_ROOT / "src/gobby/install/shared/skills"


def test_bundled_instruction_files_fit_registered_default() -> None:
    limit = SkillsConfig().bundled_max_content_size

    violations = find_bundled_content_violations(BUNDLED_SKILLS, limit)

    assert violations == []


def test_violation_reports_counts_limit_and_decomposition_guidance(tmp_path: Path) -> None:
    skill = tmp_path / "oversized"
    references = skill / "references"
    references.mkdir(parents=True)
    (skill / "SKILL.md").write_text("é" * 8, encoding="utf-8")
    (references / "topic.md").write_text("ascii" * 4, encoding="utf-8")
    (skill / "scripts").mkdir()
    (skill / "scripts" / "ignored.py").write_text("x" * 100, encoding="utf-8")

    violations = find_bundled_content_violations(tmp_path, limit=12)

    assert [(item.path.name, item.character_count, item.byte_count) for item in violations] == [
        ("SKILL.md", 8, 16),
        ("topic.md", 20, 20),
    ]
    for violation in violations:
        assert violation.limit == 12
        assert (
            "Keep SKILL.md as purpose, common path, invariants, and topic index"
            in violation.message
        )
        assert "get_skill_file" in violation.message
        assert "three-reference activation budget" in violation.message
