"""Tests for complete skill-delivery guidance in the loading-skills bundle."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_loading_skills_requires_complete_separate_results_and_retry() -> None:
    skill_path = (
        Path(__file__).parents[2]
        / "src"
        / "gobby"
        / "install"
        / "shared"
        / "skills"
        / "loading-skills"
        / "SKILL.md"
    )
    content = skill_path.read_text(encoding="utf-8")

    assert "one `get_skill` request per outer tool result" in content
    assert "fully read its complete body" in content
    assert "deduplicate names" in content
    assert "sequentially in required order" in content
    assert "Do not use `Promise.all`" in content
    assert "`structuredContent.result.skill.content`" in content
    assert "`…N tokens truncated…`" in content
    assert "retry that skill individually" in content
    assert "Collapsed UI previews are presentation-only" in content
