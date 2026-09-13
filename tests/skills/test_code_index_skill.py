"""Code-index operating contracts remain discoverable through focused references."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
REFERENCES = ROOT / "src/gobby/install/shared/skills/gobby/references/code-index"


def _body(topic: str) -> str:
    return " ".join((REFERENCES / f"{topic}.md").read_text().split())


def test_code_index_skill_documents_positional_path_filters() -> None:
    search, navigation, graphs = (_body(name) for name in ("search", "navigation", "graphs"))
    assert "positional paths/globs with OR semantics" in search
    assert "Multiple positional paths and globs use OR semantics" in navigation
    assert "Bare paths resolve from the project root" in navigation
    assert "`-m/--limit` has alias `--max-count`" in search
    assert "Gcode owns graph and vector operations" in graphs
    assert "daemon HTTP routes delegate UI operations" in graphs


def test_code_index_skill_documents_gcode_first_retrieval_workflow() -> None:
    body = _body("retrieval")
    assert "`gcode symbol-at path/to/file.py:42` after a file/line hit" in body
    assert "`gcode outline path/to/file.py`" in body
    assert "`gcode symbol <full-uuid>`" in body
    assert "`gcode symbols` for a batch" in body
    assert "Fetch tight neighboring context only when needed after retrieval" in body
    assert "do not replace this flow with whole-file reads" in body


def test_code_index_skill_front_loads_command_selection_and_lane_recovery() -> None:
    body = _body("search")
    for command in (
        'gcode search-symbol "name"',
        'gcode grep -w "identifier"',
        'gcode grep -F "spawn_ui_server("',
        'gcode search-content "text"',
        'gcode search "concept"',
    ):
        assert command in body
    assert body.index("Query shape") < body.index("Ranked search accepts")
    assert "switch according to query shape" in body
    assert "Do not paraphrase the same fuzzy query or page through noise" in body


def test_code_index_skill_documents_ast_only_outline_and_markdown_recovery() -> None:
    body = _body("retrieval")
    assert "`outline` is AST-only" in body
    assert "Markdown and other content-only files" in body
    assert "gcode grep '^#{1,6} ' path/to/file.md -m 200" in body


def test_code_index_skill_documents_allow_stale_flag() -> None:
    body = _body("recovery")
    assert "--allow-stale" in body
    assert "--no-freshness" not in body


def test_code_index_skill_documents_grep_compat_exit_and_grant_errors() -> None:
    search, recovery = _body("search"), _body("recovery")
    assert "`-l` for files" in search
    assert "`-E/-n/-r/-R` are accepted no-ops" in search
    assert "Empty success is not an error" in search
    assert "payload_skew" in recovery
    assert "api_contract_mismatch" in recovery
    assert "recovery" in recovery
    assert "--no-freshness" not in recovery


def test_code_index_skill_documents_durable_plan_targets() -> None:
    body = _body("navigation")
    assert "resolve exact qualified names before broader impact queries" in body
    assert "`path.py::Class.method`" in body
    assert "`path.rs::Type::method`" in body
    assert "Use neither UUIDs nor line numbers as durable plan targets" in body
    assert "File-wide targets need a scope reason" in body


def test_code_index_skill_documents_callees_and_graph_view() -> None:
    impact, graphs, navigation = (_body(name) for name in ("impact", "graphs", "navigation"))
    assert "`callees` for outgoing calls" in impact
    assert "2,000-token compact-text page budget" in navigation
    assert "oversized first item remains complete" in navigation
    assert "exact shell-safe continuation command unchanged" in navigation
    for expected in (
        "--view mcg",
        "--module",
        "--view fcg",
        "--view class-hierarchy",
        "depth 8",
        "no row limit within depth",
        "default to depth 1",
        "incoming/outgoing truncation",
        "nullable node `file`",
        "module aliases identify the same provider neighborhood",
    ):
        assert expected in graphs


def test_code_index_skill_prefers_compact_location_retrieval() -> None:
    assert "compact-text" in _body("navigation")
    assert "Text omits UUIDs and ranking diagnostics" in _body("search")
    assert "Request JSON or `--verbose` only when needed" in _body("search")
    assert "`gcode symbol-at path/to/file.py:42`" in _body("retrieval")


def test_code_index_skill_documents_stale_ids_and_callback_fallback() -> None:
    body = _body("retrieval")
    assert "Content-derived IDs change after edits" in body
    assert "re-resolve the file with `outline` or `symbol-at`" in body
    assert "retains valid requested bodies and reports missing IDs" in body
    impact = _body("impact")
    assert "callback references can leave gaps" in impact
    assert 'gcode grep -w "symbol_name"' in impact


def test_code_index_skill_matches_gcode_bundled_asset_when_present() -> None:
    """The native installer must carry the same thin router, not retired instructions."""
    assert (ROOT / "crates/gcode/assets/SKILL.md").read_bytes() == (
        REFERENCES.parents[1] / "SKILL.md"
    ).read_bytes()
