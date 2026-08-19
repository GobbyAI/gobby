"""Content checks for removed workflow wait-tool guidance in bundled skills."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills"
WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows"
UPDATED_SKILLS = ("expand", "plan", "build-coordinator", "goal")
WAKE_DRIVEN_GUIDANCE = (
    pytest.param(SKILLS_DIR / "build-coordinator/SKILL.md", id="build-coordinator"),
    pytest.param(SKILLS_DIR / "goal/SKILL.md", id="goal"),
    pytest.param(SKILLS_DIR / "merge-expert/SKILL.md", id="merge-expert"),
    pytest.param(SKILLS_DIR / "plan/SKILL.md", id="plan"),
    pytest.param(WORKFLOWS_DIR / "agents/goal-taskmaster.yaml", id="goal-taskmaster"),
    pytest.param(
        WORKFLOWS_DIR / "agents/merge-orchestrator.yaml",
        id="merge-orchestrator",
    ),
)
REVIEW_DISPATCH_GUIDANCE = (
    pytest.param(SKILLS_DIR / "review/SKILL.md", id="review"),
    pytest.param(WORKFLOWS_DIR / "agents/epic-reviewer.yaml", id="epic-reviewer"),
    pytest.param(WORKFLOWS_DIR / "review.yaml", id="review-workflow"),
)
CAPTURE_GUIDANCE = (
    pytest.param(SKILLS_DIR / "goal/SKILL.md", id="goal"),
    pytest.param(SKILLS_DIR / "merge-expert/SKILL.md", id="merge-expert"),
    pytest.param(SKILLS_DIR / "plan/SKILL.md", id="plan"),
)


_WHITESPACE_RUN = re.compile(r"\s+")


def _guidance_text(path: Path) -> str:
    """Read a template, collapsing whitespace runs to single spaces.

    The asserted phrases live in YAML block scalars and Markdown prose that the
    prompt style contract wraps at ~80 columns, so a phrase may straddle a line
    break and its indentation. Normalizing keeps these checks pinned to the
    guidance content rather than to its current line layout.
    """
    return _WHITESPACE_RUN.sub(" ", path.read_text())


@pytest.mark.parametrize("skill_name", UPDATED_SKILLS)
def test_skills_do_not_mention_removed_wait_tool(skill_name: str) -> None:
    body = _guidance_text(SKILLS_DIR / skill_name / "SKILL.md")

    assert "wait_for_completion" not in body
    assert "wait_timeout" not in body
    assert "wait_for_completion: true" not in body


@pytest.mark.parametrize("path", WAKE_DRIVEN_GUIDANCE)
def test_wait_guidance_is_wake_driven(path: Path) -> None:
    body = _guidance_text(path)

    assert "subscribe once" in body
    assert "end the turn" in body
    assert "daemon wake" in body
    assert "re-call" in body
    assert "sweep" in body


@pytest.mark.parametrize("path", CAPTURE_GUIDANCE)
def test_terminal_result_guidance_pages_capture_metadata(path: Path) -> None:
    body = _guidance_text(path)

    assert "capture metadata" in body
    assert "get_agent_capture" in body
    assert "complete" in body


@pytest.mark.parametrize("path", REVIEW_DISPATCH_GUIDANCE)
def test_review_dispatch_guidance_avoids_removed_wait_controls(path: Path) -> None:
    body = _guidance_text(path)

    assert "wait_for_completion" not in body
    assert "wait_timeout" not in body
    assert "timeout_seconds" not in body
