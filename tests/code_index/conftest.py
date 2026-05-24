"""Shared fixtures for code_index tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gobby.code_index.models import Symbol
from gobby.code_index.storage import CodeIndexStorage

pytestmark = pytest.mark.unit

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


@pytest.fixture
def code_db(postgres_db: HubDatabase) -> HubDatabase:
    """Database with the code index tables needed by these unit tests."""
    return postgres_db


@pytest.fixture
def code_storage(code_db: HubDatabase) -> CodeIndexStorage:
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
