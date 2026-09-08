"""Contract checks for current gcode command-selection documentation."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_current_guides_keep_symbol_and_content_search_lanes_separate() -> None:
    readme = _read("crates/gcode/README.md")
    code_index = _read("docs/guides/code-index.md")
    user_guide = _read("docs/guides/gcode-user-guide.md")
    development_guide = _read("docs/guides/gcode-development-guide.md")
    search_guide = _read("docs/guides/search.md")

    assert 'gcode search-symbol "handleAuth"' in readme
    assert "Hybrid symbol search" in code_index
    assert "Hybrid search: full-text" not in code_index
    assert "`gcode search` does not search\ncontent chunks" in user_guide
    assert "content-chunk search is a separate pipeline" in development_guide
    assert "Indexed repository symbols and content" not in search_guide
    assert "Code symbols" in search_guide
    assert "Code content" in search_guide


def test_current_guides_document_ast_only_outline_and_markdown_recovery() -> None:
    code_index = _read("docs/guides/code-index.md")
    user_guide = _read("docs/guides/gcode-user-guide.md")
    cli_contract = _read("docs/contracts/gcode-cli.md")

    for document in (code_index, user_guide, cli_contract):
        assert "AST-only" in document
        assert "gcode grep '^#{1,6} '" in document

    assert "Markdown headings, JSON/YAML" not in user_guide
    assert "Structured docs/config | Markdown, YAML, JSON" not in code_index
