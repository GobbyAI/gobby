"""Engine code names no agent definition (AGENTS.md Working Rule 16).

The workflow engine and the task landing path read authorities from a definition's YAML,
never from its name. This guard parses those modules and fails on any string literal
equal to a bundled definition name. The names come from the bundled agents directory,
so this file names none of them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_AGENTS = REPO_ROOT / "src/gobby/install/shared/workflows/agents"
GUARDED_PACKAGES = ("src/gobby/workflows", "src/gobby/tasks")
# Rule 16 exempts the `default` definition from the no-names rule.
EXEMPT_NAMES = frozenset({"default"})


def _definition_names() -> frozenset[str]:
    return frozenset(path.stem for path in BUNDLED_AGENTS.glob("*.yaml")) - EXEMPT_NAMES


def _string_literals(source: str) -> set[str]:
    """Every string constant outside a docstring or bare string statement."""
    tree = ast.parse(source)
    statement_strings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in statement_strings
    }


def test_bundled_definition_names_are_discovered() -> None:
    names = _definition_names()
    assert len(names) > 1
    assert EXEMPT_NAMES.isdisjoint(names)


def test_engine_modules_compare_no_definition_name() -> None:
    names = _definition_names()
    offenders: dict[str, list[str]] = {}
    for package in GUARDED_PACKAGES:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            hits = _string_literals(path.read_text(encoding="utf-8")) & names
            if hits:
                offenders[str(path.relative_to(REPO_ROOT))] = sorted(hits)
    assert offenders == {}
