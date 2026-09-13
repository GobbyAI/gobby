"""Live-work authorization and lifecycle survive entrypoint retirement."""

from pathlib import Path

import pytest

from gobby.skills.capability_catalog import load_capability_catalog

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills"
LIVE = ROOT / "gobby/references/tasks/live-work.md"


def test_live_session_skill_parses_with_root_interactive_metadata() -> None:
    assert (
        load_capability_catalog().folded_skills["live-session"]
        == "gobby:references/tasks/live-work.md"
    )
    content = " ".join(LIVE.read_text().split())
    assert "Only a root interactive terminal session" in content
    assert "spawned, automated, and web-chat sessions cannot" in content
    assert "starting, resuming, or finishing" in content
    assert not (ROOT / "live-session").exists()


def test_live_session_skill_defines_complete_lifecycle_and_recovery() -> None:
    content = " ".join(LIVE.read_text().split())
    for expected in (
        "refuse mixed ordinary/live claims",
        '`labels=["live-session"]`',
        "`claim=true`",
        "`allow_automation=false`",
        '`isolation="none"`',
        "`unattended=false`",
        "never create an empty commit",
        "post-close memory review",
        "escalates dirty or indeterminate",
        "Remove this extra instruction requirement only after the task closes",
    ):
        assert expected in content


def test_bridge_live_mode_delegates_lifecycle_to_live_session() -> None:
    content = " ".join((ROOT / "bridge/SKILL.md").read_text().split())
    assert 'get_skill_file(name="gobby", path="references/tasks/live-work.md")' in content
    assert "Bridge owns annotation processing" in content
    assert "Follow `gobby:references/tasks/live-work.md` to finish the live scope" in content
