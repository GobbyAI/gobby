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
    assert parsed.metadata is not None
    assert parsed.metadata["gobby"]["audience"] == "all"
    assert "bridge" in {skill.name for skill in skills}


def test_bridge_skill_live_mode_contract() -> None:
    """Live mode preserves one umbrella task and uses the verified wait contract."""
    body = _body()

    assert "## Invocation" in body
    assert "## Live Mode" in body
    assert "A live label does not waive turn-end gates" in body
    assert "applicable durable wait" in body
    assert "Monitor" in body
    assert "`gobby:references/tasks/live-work.md` reference owns the umbrella task" in body
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

    assert "Follow `gobby:references/tasks/live-work.md` to finish the live scope" in body
    assert "the final task-linked commit when changes exist, and `close_task`" in normalized
    assert "never close the task mid-session" in normalized
