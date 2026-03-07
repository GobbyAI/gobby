"""Shared fixtures for code_index tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.code_index.models import Symbol
from gobby.code_index.storage import CodeIndexStorage
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit

_CODE_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS code_indexed_projects (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    total_files INTEGER NOT NULL DEFAULT 0,
    total_symbols INTEGER NOT NULL DEFAULT 0,
    last_indexed_at TEXT,
    index_duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS code_indexed_files (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    symbol_count INTEGER NOT NULL DEFAULT 0,
    byte_size INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_cif_project ON code_indexed_files(project_id);

CREATE TABLE IF NOT EXISTS code_symbols (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    language TEXT NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    signature TEXT,
    docstring TEXT,
    parent_symbol_id TEXT,
    content_hash TEXT NOT NULL,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cs_project ON code_symbols(project_id);
CREATE INDEX IF NOT EXISTS idx_cs_file ON code_symbols(project_id, file_path);
CREATE INDEX IF NOT EXISTS idx_cs_name ON code_symbols(name);
CREATE INDEX IF NOT EXISTS idx_cs_qualified ON code_symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_cs_kind ON code_symbols(kind);
CREATE INDEX IF NOT EXISTS idx_cs_parent ON code_symbols(parent_symbol_id);
"""


@pytest.fixture
def code_db(tmp_path: Path) -> LocalDatabase:
    """Database with code index tables migrated."""
    db = LocalDatabase(tmp_path / "code-test.db")
    run_migrations(db)
    # Apply code index schema on top (idempotent via IF NOT EXISTS)
    conn = db.connection
    conn.executescript(_CODE_INDEX_SCHEMA)
    conn.commit()
    yield db  # type: ignore[misc]
    db.close()


@pytest.fixture
def code_storage(code_db: LocalDatabase) -> CodeIndexStorage:
    """CodeIndexStorage wired to the test database."""
    return CodeIndexStorage(code_db)


@pytest.fixture
def sample_python_source() -> str:
    """Realistic Python source for parser tests."""
    return '''\
"""Module docstring."""

import os
from pathlib import Path


def greet(name: str) -> str:
    """Return a greeting."""
    return f"Hello, {name}!"


class Calculator:
    """A simple calculator."""

    def add(self, a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    def multiply(self, a: int, b: int) -> int:
        """Multiply two numbers."""
        return a * b


def main() -> None:
    calc = Calculator()
    print(greet("world"))
    print(calc.add(1, 2))
'''


@pytest.fixture
def sample_symbols() -> list[Symbol]:
    """Pre-built Symbol objects for unit tests."""
    project_id = "proj-1"
    file_path = "src/app.py"
    language = "python"

    func_sym = Symbol(
        id=Symbol.make_id(project_id, file_path, "greet", "function", 50),
        project_id=project_id,
        file_path=file_path,
        name="greet",
        qualified_name="greet",
        kind="function",
        language=language,
        byte_start=50,
        byte_end=120,
        line_start=7,
        line_end=9,
        signature="def greet(name: str) -> str:",
        docstring="Return a greeting.",
        content_hash="abc123",
    )

    class_sym = Symbol(
        id=Symbol.make_id(project_id, file_path, "Calculator", "class", 130),
        project_id=project_id,
        file_path=file_path,
        name="Calculator",
        qualified_name="Calculator",
        kind="class",
        language=language,
        byte_start=130,
        byte_end=350,
        line_start=12,
        line_end=22,
        signature="class Calculator:",
        docstring="A simple calculator.",
        content_hash="def456",
    )

    method_sym = Symbol(
        id=Symbol.make_id(project_id, file_path, "add", "method", 200),
        project_id=project_id,
        file_path=file_path,
        name="add",
        qualified_name="Calculator.add",
        kind="method",
        language=language,
        byte_start=200,
        byte_end=280,
        line_start=16,
        line_end=18,
        signature="def add(self, a: int, b: int) -> int:",
        docstring="Add two numbers.",
        parent_symbol_id=class_sym.id,
        content_hash="ghi789",
    )

    return [func_sym, class_sym, method_sym]
