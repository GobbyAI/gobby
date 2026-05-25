"""Tests for bundled code-index skill guidance."""

from pathlib import Path

import pytest

from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

SKILL_PATH = Path("src/gobby/install/shared/skills/code-index/SKILL.md")


def test_code_index_skill_documents_positional_path_filters() -> None:
    """Document supported path filter syntax for gcode search commands."""
    parsed = parse_skill_file(SKILL_PATH)
    body = parsed.content

    assert parsed.name == "code-index"
    assert parsed.get_category() == "core"

    assert 'gcode search "query" [PATH ...]' in body
    assert 'gcode search-content "query" [PATH ...]' in body
    assert "--path <glob>" not in body
    assert "positional path filters" in body
    assert "code-index graph projection via the Gobby daemon" in body
