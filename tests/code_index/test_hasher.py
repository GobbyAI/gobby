"""Tests for code_index.hasher."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gobby.code_index.hasher import file_content_hash, symbol_content_hash

pytestmark = pytest.mark.unit


def test_file_content_hash_consistency(tmp_path: Path) -> None:
    """Same file content always produces the same hash."""
    f = tmp_path / "hello.py"
    f.write_text("print('hello')\n")

    h1 = file_content_hash(f)
    h2 = file_content_hash(f)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex digest


def test_file_content_hash_matches_manual(tmp_path: Path) -> None:
    """Hash matches a manually computed SHA-256."""
    content = b"def foo(): pass\n"
    f = tmp_path / "foo.py"
    f.write_bytes(content)

    expected = hashlib.sha256(content).hexdigest()
    assert file_content_hash(f) == expected


def test_file_content_hash_changes_with_content(tmp_path: Path) -> None:
    """Different content -> different hash."""
    f = tmp_path / "a.py"
    f.write_text("version = 1")
    h1 = file_content_hash(f)

    f.write_text("version = 2")
    h2 = file_content_hash(f)
    assert h1 != h2


def test_file_content_hash_accepts_str_path(tmp_path: Path) -> None:
    """Accepts a string path argument."""
    f = tmp_path / "s.py"
    f.write_text("x = 1")
    h = file_content_hash(str(f))
    assert isinstance(h, str) and len(h) == 64


def test_symbol_content_hash_correctness() -> None:
    """Hash of a byte slice matches manual computation."""
    source = b"def greet(name): return f'Hello, {name}!'"
    start, end = 0, 16  # b"def greet(name):"
    expected = hashlib.sha256(source[start:end]).hexdigest()
    assert symbol_content_hash(source, start, end) == expected


def test_symbol_content_hash_varies_with_range() -> None:
    """Different byte ranges produce different hashes."""
    source = b"class Foo:\n    def bar(self): pass"
    h1 = symbol_content_hash(source, 0, 10)
    h2 = symbol_content_hash(source, 10, 30)
    assert h1 != h2


def test_symbol_content_hash_empty_slice() -> None:
    """Empty slice produces the hash of empty bytes."""
    source = b"some code"
    expected = hashlib.sha256(b"").hexdigest()
    assert symbol_content_hash(source, 5, 5) == expected
