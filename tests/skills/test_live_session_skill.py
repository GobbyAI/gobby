"""Contract tests for the bundled live-session lifecycle skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.skills.loader import SkillLoader
from gobby.skills.parser import parse_frontmatter

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/live-session"
BRIDGE_SKILL_PATH = REPO_ROOT / "src/gobby/install/shared/skills/bridge/SKILL.md"


def test_live_session_skill_parses_with_root_interactive_metadata() -> None:
    parsed = SkillLoader().load_skill(LIVE_SKILL_DIR, validate=True)
    frontmatter, content = parse_frontmatter(
        (LIVE_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    )

    assert parsed.name == "live-session"
    assert parsed.version == "1.0.0"
    assert frontmatter["metadata"]["gobby"] == {
        "audience": "interactive",
        "depth": 0,
    }
    assert "/gobby live-session start <scope>" in content
    assert "/gobby live-session done" in content


def test_live_session_skill_defines_complete_lifecycle_and_recovery() -> None:
    content = (LIVE_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "Never mix an ordinary claim with a live-session claim." in content
    assert '`labels=["live-session"]`' in content
    assert "`claim=true`" in content
    assert "`allow_automation=false`" in content
    assert '`isolation="none"`' in content
    assert "No changes: skip commit creation." in content
    assert "`memory_review_completed=true`" in content
    assert "dirty or indeterminate claims" in content


def test_bridge_live_mode_delegates_lifecycle_to_live_session() -> None:
    content = BRIDGE_SKILL_PATH.read_text(encoding="utf-8")

    assert "Load `live-session` with `gobby-skills:get_skill`" in content
    assert '`live start "Drawbridge annotations — <scope or date>"`' in content
    assert "Execute `live done`." in content
    assert "Bridge owns annotation processing." in content
