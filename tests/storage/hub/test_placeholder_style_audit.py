from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from gobby.search.keyword import placeholder
from gobby.storage.sql_dialect import (
    json_array_contains_condition,
    newer_than_now_expr,
    older_than_now_expr,
)
from gobby.utils.sql import sql_placeholders

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCAN_ROOTS = (PROJECT_ROOT / "src" / "gobby", PROJECT_ROOT / "tests")
DB_SQL_METHODS = {"execute", "executemany", "fetchone", "fetchall"}
RAW_PERCENT_RE = re.compile(r"(?<!%)%(?!%|\(|s\b|b\b|t\b)")
DOLLAR_PLACEHOLDER_RE = re.compile(r"\$[1-9][0-9]*\b")


def test_db_call_sql_literals_use_psycopg_placeholders() -> None:
    violations: list[str] = []
    for path, lineno, sql in _db_call_sql_literals():
        scrubbed = _scrub_sql(sql)
        if "?" in scrubbed or DOLLAR_PLACEHOLDER_RE.search(scrubbed):
            violations.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}")

    assert violations == []


def test_db_call_sql_literals_escape_raw_percent_signs() -> None:
    violations: list[str] = []
    for path, lineno, sql in _db_call_sql_literals():
        if RAW_PERCENT_RE.search(_scrub_comments_and_dollar_quotes(sql)):
            violations.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}")

    assert violations == []


def test_dynamic_placeholder_helpers_emit_psycopg_placeholders() -> None:
    condition, params = json_array_contains_condition(object(), "tags", "gobby")

    assert sql_placeholders(3) == "%s,%s,%s"
    assert sql_placeholders(2, separator=", ") == "%s, %s"
    assert placeholder(object(), 1) == "%s"
    assert condition == "tags @> %s::jsonb"
    assert params == ('["gobby"]',)
    assert older_than_now_expr(object(), "updated_at", "%s", "hour") == (
        "updated_at < NOW() - (%s::double precision * INTERVAL '1 hour')"
    )
    assert newer_than_now_expr(object(), "created_at", "%s", "minute") == (
        "created_at >= NOW() - (%s::double precision * INTERVAL '1 minute')"
    )


def _db_call_sql_literals() -> list[tuple[Path, int, str]]:
    literals: list[tuple[Path, int, str]] = []
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                sql_arg_index = _sql_arg_index(node)
                if sql_arg_index is None or sql_arg_index >= len(node.args):
                    continue
                sql = _literal_sql(node.args[sql_arg_index])
                if sql is not None:
                    literals.append((path, node.lineno, sql))
    return literals


def _sql_arg_index(node: ast.Call) -> int | None:
    if node.func.attr in DB_SQL_METHODS:
        return 0
    if node.func.attr == "safe_update" and len(node.args) >= 3:
        return 2
    return None


def _literal_sql(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_sql(node.left)
        right = _literal_sql(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _scrub_sql(sql: str) -> str:
    return _scrub_quoted_strings(_scrub_comments_and_dollar_quotes(sql))


def _scrub_comments_and_dollar_quotes(sql: str) -> str:
    return re.sub(
        r"--[^\n]*(?:\n|$)|/\*.*?\*/|\$[A-Za-z_][A-Za-z0-9_]*\$.*?\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$.*?\$\$",
        " ",
        sql,
        flags=re.DOTALL,
    )


def _scrub_quoted_strings(sql: str) -> str:
    return re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", " ", sql)
