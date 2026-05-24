"""Tests for bundled code-index skill guidance."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILL_PATH = Path("src/gobby/install/shared/skills/code-index/SKILL.md")


def test_code_index_skill_documents_positional_path_filters() -> None:
    body = SKILL_PATH.read_text(encoding="utf-8")

    assert 'gcode search "query" [PATH ...]' in body
    assert 'gcode search-content "query" [PATH ...]' in body
    assert "--path <glob>" not in body
    assert "positional path filters" in body
    assert "code-index graph projection via the Gobby daemon" in body
