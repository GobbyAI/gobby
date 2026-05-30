"""Tests for bundled code-index skill guidance."""

from pathlib import Path

import pytest

from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

SKILL_PATH = Path("src/gobby/install/shared/skills/code-index/SKILL.md")
GOBBY_CLI_SKILL_PATH = Path("/Users/josh/Projects/gobby-cli/crates/gcode/assets/SKILL.md")


def test_code_index_skill_documents_positional_path_filters() -> None:
    """Document supported path filter syntax for gcode search commands."""
    parsed = parse_skill_file(SKILL_PATH)
    body = parsed.content

    assert parsed.name == "code-index"
    assert parsed.get_category() == "core"

    assert 'gcode search "query" [PATH ...]' in body
    assert 'gcode grep "pattern" [PATH ...] -m 50' in body
    assert 'gcode search-content "query" [PATH ...]' in body
    assert "-m/--max-count" in body
    assert "--format json" in body
    assert "--path <glob>" not in body
    assert "positional path filters" in body
    assert "code-index graph projection via the Gobby daemon" in body


def test_code_index_skill_documents_gcode_first_retrieval_workflow() -> None:
    """Document gcode-first navigation before falling back to line readers."""
    parsed = parse_skill_file(SKILL_PATH)
    body = parsed.content

    assert "## Recommended Workflow" in body
    assert '`gcode search "concept"`' in body
    assert '`gcode search-symbol "name"`' in body
    assert '`gcode search-content "text"`' in body
    assert "`gcode outline path/to/file`" in body
    assert "`gcode symbol <full-uuid>`" in body
    assert "`gcode symbols <full-uuid> <full-uuid> ...`" in body
    assert "Search output is intentionally snippet-sized" in body
    assert "`gsqz`" in body
    assert "use `sed`/`awk` only for tight neighboring context (1-3 lines)" in body


def test_code_index_skill_matches_gcode_bundled_asset_when_present() -> None:
    """Keep Gobby's install template byte-identical to gcode's bundled skill."""
    if not GOBBY_CLI_SKILL_PATH.exists():
        pytest.skip("gobby-cli checkout is not present")

    assert SKILL_PATH.read_bytes() == GOBBY_CLI_SKILL_PATH.read_bytes()
