"""Contract tests for the bundled bridge skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.skills.loader import SkillLoader
from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/bridge"
SKILLS_ROOT = REPO_ROOT / "src/gobby/install/shared/skills"


def _body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_bridge_skill_parses_and_is_discoverable() -> None:
    """Verify the bridge SKILL.md parses and is discoverable by the skill loader."""
    parsed = parse_skill_file(SKILL_DIR / "SKILL.md")
    skills = SkillLoader().load_directory(SKILLS_ROOT)

    assert parsed.name == "bridge"
    assert parsed.metadata["gobby"]["audience"] == "all"
    assert "bridge" in {skill.name for skill in skills}


def test_bridge_skill_live_mode_contract() -> None:
    """Live mode keeps one umbrella task inside one turn until a sentinel ends it."""
    body = _body()

    assert "## Invocation" in body
    assert "## Live Mode" in body
    assert "Never end the turn to wait" in body
    assert "Monitor" in body
    assert "create_task" in body
    assert "close_task" in body
    # Sentinel matcher tokens
    assert "`done`" in body
    assert "`stop`" in body
    assert "`end session`" in body
    # Detection contract: statuses, never array position/count
    assert "to do" in body
    assert "processed" in body


def test_bridge_skill_live_mode_single_commit_wrapup() -> None:
    """Wrap-up commits the session diff before closing the umbrella task."""
    body = _body()

    assert "single commit" in body
    assert "commit SHA" in body
