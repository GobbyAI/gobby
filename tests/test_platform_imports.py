"""Windows import safety for POSIX-only standard library modules.

The gobby CLI and daemon startup import most of ``src/gobby``. One module-scope
import of a POSIX-only module (``fcntl``, ``termios``, ``pwd``, ...) fails that
whole import chain on Windows before the daemon exists. Guard such imports with an
``os.name``/``sys.platform`` check or an ``ImportError`` handler, or import them
inside the function that needs them.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[1] / "src" / "gobby"
POSIX_ONLY_MODULES = frozenset({"fcntl", "grp", "pty", "pwd", "resource", "termios", "tty"})
IMPORT_ERRORS = frozenset({"ImportError", "ModuleNotFoundError"})


def _is_import_guard(test: ast.expr) -> bool:
    """Platform checks and TYPE_CHECKING blocks never run a POSIX import on Windows."""
    return any(
        (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING")
        or (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and (node.value.id, node.attr) in {("os", "name"), ("sys", "platform")}
        )
        for node in ast.walk(test)
    )


def _handles_import_error(statement: ast.Try) -> bool:
    return any(
        handler.type is None
        or any(
            isinstance(node, ast.Name) and node.id in IMPORT_ERRORS
            for node in ast.walk(handler.type)
        )
        for handler in statement.handlers
    )


def _import_time_imports(body: list[ast.stmt]) -> Iterator[ast.Import | ast.ImportFrom]:
    """Yield imports that execute at module import without a platform or ImportError guard."""
    for statement in body:
        if isinstance(statement, ast.Import | ast.ImportFrom):
            yield statement
        elif isinstance(statement, ast.If):
            if not _is_import_guard(statement.test):
                yield from _import_time_imports(statement.body)
                yield from _import_time_imports(statement.orelse)
        elif isinstance(statement, ast.Try):
            if not _handles_import_error(statement):
                yield from _import_time_imports(statement.body)
            for handler in statement.handlers:
                yield from _import_time_imports(handler.body)
            yield from _import_time_imports(statement.orelse)
            yield from _import_time_imports(statement.finalbody)


def _posix_only_lines(source: str) -> list[int]:
    lines = []
    for node in _import_time_imports(ast.parse(source).body):
        if isinstance(node, ast.ImportFrom):
            roots = {node.module.split(".")[0]} if node.module and node.level == 0 else set()
        else:
            roots = {alias.name.split(".")[0] for alias in node.names}
        if roots & POSIX_ONLY_MODULES:
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    [
        ("import fcntl\n", [1]),
        ("from pwd import getpwuid\n", [1]),
        ("import os\nif os.getenv('X'):\n    import termios\n", [3]),
        ("try:\n    import fcntl\nexcept OSError:\n    pass\n", [2]),
        (
            "import sys\nif sys.platform == 'win32':\n    import msvcrt\nelse:\n    import fcntl\n",
            [],
        ),
        ("import os\nif os.name == 'posix':\n    import fcntl as _fcntl\n", []),
        ("try:\n    import termios\nexcept ImportError:\n    termios = None\n", []),
        ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import resource\n", []),
        ("def lock():\n    import fcntl\n", []),
    ],
)
def test_detector_flags_only_unguarded_import_time_imports(
    source: str, expected_lines: list[int]
) -> None:
    assert _posix_only_lines(source) == expected_lines


def test_src_has_no_unguarded_posix_only_imports() -> None:
    sources = sorted(SRC.rglob("*.py"))
    assert sources, f"no Python sources found under {SRC}"
    offenders = [
        f"{path.relative_to(SRC.parents[1])}:{line}"
        for path in sources
        for line in _posix_only_lines(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "POSIX-only modules imported at module scope break the gobby CLI and daemon "
        "startup on Windows; guard them with sys.platform/os.name or import them where used"
    )
