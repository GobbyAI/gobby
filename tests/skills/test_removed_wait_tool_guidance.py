"""Content checks for removed workflow wait-tool guidance in bundled skills."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills"
WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows"
UPDATED_SKILLS = ("expand", "plan", "build-coordinator", "goal")
WAKE_DRIVEN_GUIDANCE = (
    pytest.param(SKILLS_DIR / "build-coordinator/SKILL.md", id="build-coordinator"),
    pytest.param(SKILLS_DIR / "goal/SKILL.md", id="goal"),
    pytest.param(SKILLS_DIR / "plan/SKILL.md", id="plan"),
    pytest.param(WORKFLOWS_DIR / "agents/goal-taskmaster.yaml", id="goal-taskmaster"),
)


@pytest.mark.parametrize("skill_name", UPDATED_SKILLS)
def test_skills_do_not_mention_removed_wait_tool(skill_name: str) -> None:
    body = (SKILLS_DIR / skill_name / "SKILL.md").read_text()

    assert "wait_for_completion" not in body
    assert "wait_timeout" not in body
    assert "wait_for_completion: true" not in body
    assert "timeout_seconds" not in body


@pytest.mark.parametrize("path", WAKE_DRIVEN_GUIDANCE)
def test_wait_guidance_is_wake_driven(path: Path) -> None:
    body = path.read_text()

    assert "subscribe once" in body
    assert "end the turn" in body
    assert "daemon wake" in body
    assert "re-call" in body
    assert "full status and health sweep" in body
    assert "timeout_seconds" not in body
