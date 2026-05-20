from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[3]
HUB_STORAGE_ROOT = PROJECT_ROOT / "src" / "gobby" / "storage" / "hub"
EXECUTE_METHODS = {"execute", "executemany"}


def test_hub_storage_execute_calls_use_numbered_placeholders() -> None:
    violations: list[str] = []
    for path in sorted(HUB_STORAGE_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr not in EXECUTE_METHODS:
                continue

            sql = _literal_sql(node.args[0])
            if sql is None:
                continue
            if "?" in sql or "%s" in sql:
                violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

    assert violations == []


def _literal_sql(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
        return "".join(parts)
    return None
