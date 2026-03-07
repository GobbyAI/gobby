"""Tests for code_index.compressor (PostToolUse Read output compression)."""

from __future__ import annotations

import pytest

from gobby.code_index.compressor import CodeIndexCompressor, _MIN_OUTPUT_LENGTH
from gobby.code_index.models import Symbol
from gobby.code_index.storage import CodeIndexStorage

pytestmark = pytest.mark.unit


@pytest.fixture
def compressor(code_storage: CodeIndexStorage) -> CodeIndexCompressor:
    """Compressor with default settings."""
    return CodeIndexCompressor(storage=code_storage)


# ── Returns None for small outputs ──────────────────────────────────────


def test_returns_none_for_small_output(
    compressor: CodeIndexCompressor,
) -> None:
    """Output shorter than threshold is not compressed."""
    small_output = "line 1\nline 2\nline 3\n"
    result = compressor.compress_read_output(
        "src/app.py", small_output, "proj-1"
    )
    assert result is None


def test_returns_none_below_min_length(
    compressor: CodeIndexCompressor,
) -> None:
    """Output exactly at threshold - 1 is not compressed."""
    output = "x" * (_MIN_OUTPUT_LENGTH - 1)
    result = compressor.compress_read_output(
        "src/app.py", output, "proj-1"
    )
    assert result is None


# ── Returns None for non-indexed files ──────────────────────────────────


def test_returns_none_for_non_indexed_file(
    compressor: CodeIndexCompressor,
) -> None:
    """Large output for a non-indexed file is not compressed."""
    large_output = "x = 1\n" * 5000
    result = compressor.compress_read_output(
        "unknown_file.py", large_output, "proj-1"
    )
    assert result is None


# ── Compresses large indexed file output ────────────────────────────────


def test_compresses_large_indexed_output(
    compressor: CodeIndexCompressor,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Large output for an indexed file gets compressed."""
    code_storage.upsert_symbols(sample_symbols)

    # Generate output larger than threshold
    large_output = "# " + "x" * 100 + "\n"
    large_output *= (_MIN_OUTPUT_LENGTH // len(large_output)) + 1
    assert len(large_output) >= _MIN_OUTPUT_LENGTH

    result = compressor.compress_read_output(
        "src/app.py", large_output, "proj-1"
    )
    assert result is not None
    assert result.compressed_chars < result.original_chars
    assert result.symbols_shown == 3
    assert result.savings_pct > 0


def test_compressed_output_includes_header(
    compressor: CodeIndexCompressor,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Compressed output includes file header and symbol outline."""
    code_storage.upsert_symbols(sample_symbols)

    large_output = "line\n" * 5000
    result = compressor.compress_read_output(
        "src/app.py", large_output, "proj-1"
    )
    assert result is not None

    compressed = result.compressed
    assert "src/app.py" in compressed
    assert "Symbol Outline" in compressed
    assert "3 symbols" in compressed


# ── Outline format ──────────────────────────────────────────────────────


def test_outline_includes_symbol_signatures(
    compressor: CodeIndexCompressor,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Outline shows symbol names and signatures."""
    code_storage.upsert_symbols(sample_symbols)

    large_output = "y = 2\n" * 5000
    result = compressor.compress_read_output(
        "src/app.py", large_output, "proj-1"
    )
    assert result is not None

    compressed = result.compressed
    assert "greet" in compressed
    assert "Calculator" in compressed
    assert "add" in compressed


def test_outline_indents_methods(
    compressor: CodeIndexCompressor,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Methods (with parent_symbol_id) are indented more than top-level symbols."""
    code_storage.upsert_symbols(sample_symbols)

    large_output = "z = 3\n" * 5000
    result = compressor.compress_read_output(
        "src/app.py", large_output, "proj-1"
    )
    assert result is not None

    # Methods should use 4-space indent, top-level 2-space
    lines = result.compressed.split("\n")
    method_lines = [l for l in lines if "add" in l and "method" in l]
    top_lines = [l for l in lines if "greet" in l and "function" in l]

    if method_lines:
        assert method_lines[0].startswith("    ")
    if top_lines:
        assert top_lines[0].startswith("  ") and not top_lines[0].startswith("    ")


def test_outline_shows_line_ranges(
    compressor: CodeIndexCompressor,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """Outline shows line ranges like [L7-L9]."""
    code_storage.upsert_symbols(sample_symbols)

    large_output = "w = 4\n" * 5000
    result = compressor.compress_read_output(
        "src/app.py", large_output, "proj-1"
    )
    assert result is not None
    # Check for line range format
    assert "[L" in result.compressed


# ── savings_pct ─────────────────────────────────────────────────────────


def test_savings_pct_calculation(
    compressor: CodeIndexCompressor,
    code_storage: CodeIndexStorage,
    sample_symbols: list[Symbol],
) -> None:
    """savings_pct is computed correctly."""
    code_storage.upsert_symbols(sample_symbols)

    large_output = "data\n" * 10000
    result = compressor.compress_read_output(
        "src/app.py", large_output, "proj-1"
    )
    assert result is not None
    expected_pct = round(
        (1 - result.compressed_chars / result.original_chars) * 100, 1
    )
    assert result.savings_pct == expected_pct


def test_savings_pct_zero_for_empty_original() -> None:
    """savings_pct returns 0.0 when original_chars is 0."""
    from gobby.code_index.compressor import CompressResult

    r = CompressResult(compressed="", original_chars=0, compressed_chars=0, symbols_shown=0)
    assert r.savings_pct == 0.0
