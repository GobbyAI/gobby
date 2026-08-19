"""Tests for bundled code-index skill guidance."""

import os
from pathlib import Path

import pytest

from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

SKILL_PATH = Path("src/gobby/install/shared/skills/code-index/SKILL.md")
GCODE_SKILL_PATH_ENV = "GOBBY_GCODE_SKILL_PATH"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _gcode_bundled_skill_path() -> Path:
    configured_path = os.environ.get(GCODE_SKILL_PATH_ENV)
    if configured_path:
        return Path(configured_path).expanduser()
    return REPO_ROOT / "crates/gcode/assets/SKILL.md"


def test_code_index_skill_documents_positional_path_filters() -> None:
    """Document supported path filter syntax for gcode search commands."""
    parsed = parse_skill_file(SKILL_PATH)
    body = parsed.content

    assert parsed.name == "code-index"
    assert parsed.get_category() == "core"

    assert 'gcode search "query" [PATH ...]' in body
    assert 'gcode grep "regex" [PATH ...] -m 50' in body
    assert 'gcode search-content "query" [PATH ...]' in body
    assert "-m/--max-count" in body
    assert "--format json" in body
    assert "--path <glob>" not in body
    assert "positional path filters" in body
    assert "Use `gcode` directly for the code-index graph projection." in body
    assert "graph sync/read/lifecycle behavior lives in `gcode`" in body
    assert "via the Gobby daemon" not in body


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
    assert "before reaching for broad `sed`, `awk`, or full-file reads" in body
    assert "use `sed`/`awk` only for tight neighboring context (1-3 lines)" in body


def test_code_index_skill_documents_allow_stale_flag() -> None:
    body = parse_skill_file(SKILL_PATH).content

    assert "--allow-stale" in body
    assert "--no-freshness" not in body


def test_code_index_skill_documents_grep_compat_exit_and_grant_errors() -> None:
    body = parse_skill_file(SKILL_PATH).content

    assert "-l/--files-with-matches" in body
    assert "`-E`, `-n`, `-r`, and `-R` are accepted no-ops" in body
    assert "one-line JSON usage error" in body
    assert "Exit 0 always means success, including empty results" in body
    assert "payload_skew" in body
    assert "api_contract_mismatch" in body
    assert "stop retrying gcode" in body
    assert "`recovery` directive" in body
    assert "--no-freshness" not in body


def test_code_index_skill_documents_durable_plan_targets() -> None:
    body = parse_skill_file(SKILL_PATH).content

    assert "## Plan Target References" in body
    assert "`path/to/file.py::Class.method`" in body
    assert "`path/to/file.rs::Type::method`" in body
    assert "`path/to/file.py::* — scope-reason: <non-empty explanation>`" in body
    assert "Never use the returned symbol" in body
    resolve_position = body.index("Resolve each changed symbol")
    usages_position = body.index("`gcode usages`", resolve_position)
    blast_position = body.index("`gcode blast-radius`", resolve_position)
    assert resolve_position < usages_position
    assert resolve_position < blast_position


def test_code_index_skill_documents_paused_gwiki_code_lifecycle() -> None:
    body = parse_skill_file(SKILL_PATH).content

    assert "## CodeWiki Lifecycle" in body
    assert "`gwiki code`" in body
    assert "isolated/manual use" in body
    assert "operationally paused" in body
    assert "docs/guides/codewiki.md" in body
    assert "gcode codewiki" not in body


def test_code_index_skill_matches_gcode_bundled_asset_when_present() -> None:
    """Keep Gobby's install template byte-identical to gcode's bundled skill."""
    assert SKILL_PATH.read_bytes() == _gcode_bundled_skill_path().read_bytes()
