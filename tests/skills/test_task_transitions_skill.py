"""Contract tests for bundled task-transition guidance."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "skills"
    / "task-transitions"
    / "SKILL.md"
)


def test_codex_validation_guidance_matches_literal_command_collector() -> None:
    content = SKILL_PATH.read_text()

    assert "one top-level `functions.exec` call per validation command" in content
    assert 'tools.exec_command({cmd:"GOBBY_TEST_PROTECT=1 uv run pytest' in content
    assert "text(JSON.stringify(result))" in content
    assert "`functions.wait`" in content
    assert "Promise.all(commands.map" not in content
