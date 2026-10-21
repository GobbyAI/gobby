"""Discovery methodologies remain available after their stage agents retire."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills"


def _skill_text(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("skill", "section"),
    [
        ("ideate", "Discovery Brief"),
        ("architecture", "Architecture Brief"),
        ("prd", "Product Reference Document"),
    ],
)
def test_discovery_methodology_skill_exists(skill: str, section: str) -> None:
    text = _skill_text(skill)

    assert f"name: {skill}" in text
    assert "internal: true" in text
    assert f"## {section}" in text
    assert "methodology" in text.lower()
