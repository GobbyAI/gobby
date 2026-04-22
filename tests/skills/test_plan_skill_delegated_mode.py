"""Content tests for delegated mode in /gobby plan skill."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILL_PATH = Path("src/gobby/install/shared/skills/plan/SKILL.md")


@pytest.fixture(scope="module")
def body() -> str:
    return SKILL_PATH.read_text()


def test_menu_exposes_interactive_delegated_and_plain(body: str) -> None:
    assert "Interactive" in body
    assert "Delegated" in body
    assert "Plain" in body
    assert 'value="adversarial" | "delegated" | "plain"' in body


def test_pre_set_plan_review_mode_skips_menu(body: str) -> None:
    assert 'If `plan_review_mode` is already set' in body
    assert '"adversarial"' in body
    assert '"delegated"' in body
    assert '"plain"' in body


def test_step_7_requires_artifact_path(body: str) -> None:
    assert "## Step 7: Review Loop" in body
    assert "### 7.0. Artifact precondition" in body
    assert "artifact_path is missing" in body


def test_delegated_mode_skips_per_round_plan_mode_reentry(body: str) -> None:
    assert 'If `plan_review_mode == "delegated"`' in body
    assert "without re-entering plan mode" in body
    assert "Do not interrupt" in body
    assert "non-terminal review rejections" in body
