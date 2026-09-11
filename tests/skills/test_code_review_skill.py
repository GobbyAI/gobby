"""Tests for the bundled code-review skill text."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILL_PATH = (
    Path(__file__).parents[2]
    / "src"
    / "gobby"
    / "install"
    / "shared"
    / "skills"
    / "code-review"
    / "SKILL.md"
)


def _content() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def test_code_review_skill_declares_required_skills() -> None:
    content = _content()

    assert "REQUIRED SKILL: review-learning." in content
    assert "REQUIRED SKILL: code-index." in content
    assert "Load `code-index` before the first `gcode` or file read" in content
    assert '`get_tool_schema(server_name="gobby-skills", tool_name="get_skill_file")`' in content
    assert "Pass `language` (and `repo` when known)" in content


def test_code_review_skill_handles_truncated_diffs() -> None:
    content = _content()

    assert "`... (N additions truncated)`" in content
    assert "`[full diff: rtk git diff --no-compact]`" in content
    assert "rerun the printed full-diff command" in content
    assert "marked `skipped` with that reason, never `reviewed`" in content
