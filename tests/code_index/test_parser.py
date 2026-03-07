"""Tests for code_index.parser (tree-sitter AST parsing)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.code_index.parser import CodeParser
from gobby.config.code_index import CodeIndexConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def parser() -> CodeParser:
    """CodeParser with default config."""
    return CodeParser(CodeIndexConfig())


@pytest.fixture
def python_file(tmp_path: Path, sample_python_source: str) -> Path:
    """Write sample Python source to a temp file."""
    f = tmp_path / "app.py"
    f.write_text(sample_python_source)
    return f


# ── Basic parsing ───────────────────────────────────────────────────────


def test_parse_python_extracts_functions(
    parser: CodeParser, python_file: Path, tmp_path: Path
) -> None:
    """Parser extracts top-level functions."""
    result = parser.parse_file(str(python_file), "proj", str(tmp_path))
    assert result is not None

    names = {s.name for s in result.symbols}
    assert "greet" in names
    assert "main" in names


def test_parse_python_extracts_classes(
    parser: CodeParser, python_file: Path, tmp_path: Path
) -> None:
    """Parser extracts class definitions."""
    result = parser.parse_file(str(python_file), "proj", str(tmp_path))
    assert result is not None

    classes = [s for s in result.symbols if s.kind == "class"]
    assert len(classes) == 1
    assert classes[0].name == "Calculator"


def test_parse_python_extracts_methods(
    parser: CodeParser, python_file: Path, tmp_path: Path
) -> None:
    """Parser extracts methods and links them to parent class."""
    result = parser.parse_file(str(python_file), "proj", str(tmp_path))
    assert result is not None

    methods = [s for s in result.symbols if s.kind == "method"]
    assert len(methods) == 2
    method_names = {m.name for m in methods}
    assert "add" in method_names
    assert "multiply" in method_names


# ── Parent linking ──────────────────────────────────────────────────────


def test_parent_linking(
    parser: CodeParser, python_file: Path, tmp_path: Path
) -> None:
    """Methods have parent_symbol_id set to their enclosing class."""
    result = parser.parse_file(str(python_file), "proj", str(tmp_path))
    assert result is not None

    class_sym = next(s for s in result.symbols if s.kind == "class")
    methods = [s for s in result.symbols if s.kind == "method"]

    for m in methods:
        assert m.parent_symbol_id == class_sym.id


def test_qualified_name_for_methods(
    parser: CodeParser, python_file: Path, tmp_path: Path
) -> None:
    """Methods get qualified names like ClassName.method_name."""
    result = parser.parse_file(str(python_file), "proj", str(tmp_path))
    assert result is not None

    add_method = next(s for s in result.symbols if s.name == "add")
    assert add_method.qualified_name == "Calculator.add"


# ── Docstring extraction ───────────────────────────────────────────────


def test_docstring_extraction(
    parser: CodeParser, python_file: Path, tmp_path: Path
) -> None:
    """Parser extracts docstrings from functions and classes."""
    result = parser.parse_file(str(python_file), "proj", str(tmp_path))
    assert result is not None

    greet = next(s for s in result.symbols if s.name == "greet")
    assert greet.docstring is not None
    assert "greeting" in greet.docstring.lower()

    calc = next(s for s in result.symbols if s.name == "Calculator")
    assert calc.docstring is not None
    assert "calculator" in calc.docstring.lower()


# ── Import extraction ──────────────────────────────────────────────────


def test_import_extraction(
    parser: CodeParser, python_file: Path, tmp_path: Path
) -> None:
    """Parser extracts import statements."""
    result = parser.parse_file(str(python_file), "proj", str(tmp_path))
    assert result is not None

    # The sample source has: import os, from pathlib import Path
    assert len(result.imports) >= 2


# ── Call extraction ─────────────────────────────────────────────────────


def test_call_extraction(
    parser: CodeParser, python_file: Path, tmp_path: Path
) -> None:
    """Parser extracts function/method calls."""
    result = parser.parse_file(str(python_file), "proj", str(tmp_path))
    assert result is not None

    callee_names = {c.callee_name for c in result.calls}
    # main() calls Calculator(), greet(), print(), calc.add()
    assert "greet" in callee_names
    assert "print" in callee_names


# ── Skip conditions ─────────────────────────────────────────────────────


def test_skip_binary_file(
    parser: CodeParser, tmp_path: Path
) -> None:
    """Binary files are skipped."""
    f = tmp_path / "binary.py"
    f.write_bytes(b"\x89PNG\x00\x00\x00data")
    result = parser.parse_file(str(f), "proj", str(tmp_path))
    assert result is None


def test_skip_excluded_pattern(tmp_path: Path) -> None:
    """Files matching exclude patterns are skipped."""
    node_modules = tmp_path / "node_modules" / "pkg"
    node_modules.mkdir(parents=True)
    f = node_modules / "index.js"
    f.write_text("function foo() {}")

    p = CodeParser(CodeIndexConfig(exclude_patterns=["node_modules"]))
    result = p.parse_file(str(f), "proj", str(tmp_path))
    assert result is None


def test_skip_secret_extension(
    parser: CodeParser, tmp_path: Path
) -> None:
    """Files with secret extensions are skipped."""
    f = tmp_path / "credentials.json"
    f.write_text('{"key": "secret"}')
    result = parser.parse_file(str(f), "proj", str(tmp_path))
    assert result is None


def test_skip_empty_file(
    parser: CodeParser, tmp_path: Path
) -> None:
    """Empty files are skipped."""
    f = tmp_path / "empty.py"
    f.write_text("")
    result = parser.parse_file(str(f), "proj", str(tmp_path))
    assert result is None


def test_skip_unsupported_language(
    parser: CodeParser, tmp_path: Path
) -> None:
    """Files with unsupported extensions are skipped."""
    f = tmp_path / "data.csv"
    f.write_text("a,b,c\n1,2,3")
    result = parser.parse_file(str(f), "proj", str(tmp_path))
    assert result is None


def test_skip_oversized_file(tmp_path: Path) -> None:
    """Files exceeding max size are skipped."""
    f = tmp_path / "big.py"
    f.write_text("x = 1\n" * 10)

    # Set max file size to something tiny
    config = CodeIndexConfig(max_file_size_bytes=10)
    p = CodeParser(config)
    result = p.parse_file(str(f), "proj", str(tmp_path))
    assert result is None


# ── Signature extraction ────────────────────────────────────────────────


def test_signature_extracted(
    parser: CodeParser, python_file: Path, tmp_path: Path
) -> None:
    """Symbols have a signature (first line of definition)."""
    result = parser.parse_file(str(python_file), "proj", str(tmp_path))
    assert result is not None

    greet = next(s for s in result.symbols if s.name == "greet")
    assert greet.signature is not None
    assert "def greet" in greet.signature


# ── Content hash ────────────────────────────────────────────────────────


def test_content_hash_set(
    parser: CodeParser, python_file: Path, tmp_path: Path
) -> None:
    """Each symbol has a non-empty content hash."""
    result = parser.parse_file(str(python_file), "proj", str(tmp_path))
    assert result is not None

    for sym in result.symbols:
        assert sym.content_hash != ""
        assert len(sym.content_hash) == 64  # SHA-256


# ── File hash helper ────────────────────────────────────────────────────


def test_get_file_hash(parser: CodeParser, python_file: Path) -> None:
    """get_file_hash returns hash for readable files."""
    h = parser.get_file_hash(str(python_file))
    assert h is not None
    assert len(h) == 64


def test_get_file_hash_missing(parser: CodeParser) -> None:
    """get_file_hash returns None for missing files."""
    assert parser.get_file_hash("/nonexistent/path.py") is None
