"""Content checks for removed workflow wait-tool guidance in bundled skills."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills"
WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows"
UPDATED_SKILLS = ("plan/expansion.md", "plan/review.md", "build/coordination.md")
WAKE_DRIVEN_GUIDANCE = (
    pytest.param(SKILLS_DIR / "gobby/references/build/coordination.md", id="build-coordinator"),
    pytest.param(
        SKILLS_DIR / "gobby/references/source-control/merge-campaigns.md", id="merge-expert"
    ),
    pytest.param(SKILLS_DIR / "gobby/references/plan/review.md", id="plan"),
    pytest.param(
        WORKFLOWS_DIR / "agents/merge-orchestrator.yaml",
        id="merge-orchestrator",
    ),
)
REVIEW_DISPATCH_GUIDANCE = (
    pytest.param(SKILLS_DIR / "gobby/references/review/epic.md", id="review"),
    pytest.param(WORKFLOWS_DIR / "agents/epic-reviewer.yaml", id="epic-reviewer"),
    pytest.param(WORKFLOWS_DIR / "review.yaml", id="review-workflow"),
)
CAPTURE_GUIDANCE = (
    pytest.param(
        SKILLS_DIR / "gobby/references/source-control/merge-campaigns.md", id="merge-expert"
    ),
    pytest.param(SKILLS_DIR / "gobby/references/plan/review.md", id="plan"),
)


_WHITESPACE_RUN = re.compile(r"\s+")


def _guidance_text(path: Path) -> str:
    """Read a template, collapsing whitespace runs to single spaces.

    The asserted phrases live in YAML block scalars and Markdown prose that the
    prompt style contract wraps at ~80 columns, so a phrase may straddle a line
    break and its indentation. Normalizing keeps these checks pinned to the
    guidance content rather than to its current line layout.
    """
    content = path.read_text()
    references = path.parent / "references"
    if references.is_dir():
        content = "\n\n".join(
            [content, *(reference.read_text() for reference in sorted(references.glob("*.md")))]
        )
    return _WHITESPACE_RUN.sub(" ", content)


@pytest.mark.parametrize("skill_name", UPDATED_SKILLS)
def test_skills_do_not_mention_removed_wait_tool(skill_name: str) -> None:
    body = _guidance_text(SKILLS_DIR / "gobby/references" / skill_name)

    assert "wait_for_completion" not in body
    assert "wait_timeout" not in body
    assert "wait_for_completion: true" not in body


@pytest.mark.parametrize("path", WAKE_DRIVEN_GUIDANCE)
def test_wait_guidance_is_wake_driven(path: Path) -> None:
    body = _guidance_text(path)

    if path.suffix == ".yaml":
        for term in ("subscribe once", "end the turn", "daemon wake", "re-call", "sweep"):
            assert term in body
    else:
        waits = _guidance_text(SKILLS_DIR / "gobby/references/sessions/waits.md")
        assert "wait_for_agent" in body or "event-driven waits" in body
        assert "once" in waits
        assert "yield the turn" in waits
        assert "completion wake the session" in waits
        assert "instead of polling status or registering repeatedly" in waits


@pytest.mark.parametrize("path", CAPTURE_GUIDANCE)
def test_terminal_result_guidance_pages_capture_metadata(path: Path) -> None:
    body = _guidance_text(path)

    lifecycle = _guidance_text(SKILLS_DIR / "gobby/references/agents/lifecycle.md")
    assert "complete" in body.lower()
    assert "get_agent_capture" in lifecycle
    assert "capture metadata" in lifecycle
    assert "every page" in lifecycle


@pytest.mark.parametrize("path", REVIEW_DISPATCH_GUIDANCE)
def test_review_dispatch_guidance_avoids_removed_wait_controls(path: Path) -> None:
    body = _guidance_text(path)

    assert "wait_for_completion" not in body
    assert "wait_timeout" not in body
    assert "timeout_seconds" not in body
