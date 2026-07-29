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
    # a46e947fe moved the umbrella-task lifecycle into the `live-session` skill;
    # bridge delegates rather than calling create_task itself.
    assert "`live-session` skill owns the umbrella task" in body
    assert "task creation, and claiming" in body
    assert "close_task" in body
    # Sentinel matcher tokens
    assert "`done`" in body
    assert "`stop`" in body
    assert "`end session`" in body
    # Detection contract: statuses, never array position/count
    assert "to do" in body
    assert "processed" in body


def test_bridge_skill_reconciles_interrupted_doing_entries() -> None:
    """Pre-existing in-progress entries are recovered instead of skipped."""
    body = _body()
    normalized = " ".join(body.split())

    assert "Reconcile interrupted work" in body
    assert 'pre-existing `"doing"` entry' in normalized
    assert 'finish it and mark it `"done"`' in normalized
    assert 'reset it to `"to do"`' in normalized


def test_bridge_skill_uses_tool_waits_without_sleep_loops() -> None:
    """Non-Claude harnesses use process waiting instead of shell sleep polling."""
    body = _body()

    assert "process-wait tool" in body
    assert "Do not run shell sleep loops" in body
    assert "sleep 5" not in body


def test_bridge_skill_live_mode_single_commit_wrapup() -> None:
    """Wrap-up commits the session diff before closing the umbrella task."""
    body = _body()

    normalized = " ".join(body.split())

    assert "Execute `live done`" in body
    assert "the final task-linked commit when changes exist, and `close_task`" in normalized
    assert "never close the task mid-session" in normalized
