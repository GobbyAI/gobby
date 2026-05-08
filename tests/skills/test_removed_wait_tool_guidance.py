"""Content checks for removed workflow wait-tool guidance in bundled skills."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills"
UPDATED_SKILLS = ("expand", "plan")


@pytest.mark.parametrize("skill_name", UPDATED_SKILLS)
def test_skills_do_not_mention_removed_wait_tool(skill_name: str) -> None:
    body = (SKILLS_DIR / skill_name / "SKILL.md").read_text()

    assert "wait_for_completion" not in body
    assert "wait_timeout" not in body
    assert "wait_for_completion: true" not in body
