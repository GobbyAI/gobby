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


def test_validation_guidance_is_provider_neutral_and_source_aware() -> None:
    content = SKILL_PATH.read_text()

    assert "Run each validation command as one native terminal invocation" in content
    assert "Follow every returned wait or polling token until the terminal result" in content
    assert "Recovery by captured source" in content
    assert "Claude Code and Qwen" in content
    assert "Droid and Grok" in content
    assert "Codex" in content
    assert "Manual `validation_command` evidence is prohibited" in content
