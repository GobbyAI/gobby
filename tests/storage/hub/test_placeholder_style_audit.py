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
# PostgreSQL's jsonb key-exists operator (``col ? %s`` / ``col ? 'key'``) is
# legitimate SQL, not a qmark placeholder: a qmark placeholder is never
# directly followed by a bound %s parameter or a quoted literal.
JSONB_KEY_EXISTS_RE = re.compile(r"\?(?=\s*(?:%s|'))")
# ``execute`` is not a database-only method name: WebhookTransport, the Linear
# GraphQL client, and the AI tool runtime all expose one, so this audit also
# collects their literal first arguments. A URL's query separator and its
# percent-encoded octets are not psycopg placeholders, and no real SQL
# statement binds a parameter inside a ``scheme://host`` run, so scrubbing URLs
# removes those false positives without narrowing SQL coverage. The run stops
# at either quote character so a URL inside a quoted SQL literal cannot consume
# its closing quote.
_URL_PATTERN = r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"]*"
_COMMENT_PATTERN = (
    r"--[^\n]*(?:\n|$)"
    r"|/\*.*?\*/"
    r"|\$[A-Za-z_][A-Za-z0-9_]*\$.*?\$[A-Za-z_][A-Za-z0-9_]*\$"
    r"|\$\$.*?\$\$"
)
NON_PLACEHOLDER_RE = re.compile(f"{_COMMENT_PATTERN}|{_URL_PATTERN}", re.DOTALL)


def test_db_call_sql_literals_use_psycopg_placeholders() -> None:
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}:{lineno}"
        for path, lineno, sql in _db_call_sql_literals()
        if _has_foreign_placeholder(sql)
    ]

    assert violations == []


def test_db_call_sql_literals_escape_raw_percent_signs() -> None:
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}:{lineno}"
        for path, lineno, sql in _db_call_sql_literals()
        if _has_unescaped_percent(sql)
    ]

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


def test_url_scrub_hides_only_urls_and_not_real_placeholder_bugs() -> None:
    """The URL scrub must clear webhook literals without masking real SQL bugs.

    Both assertions run the same `_has_foreign_placeholder` /
    `_has_unescaped_percent` predicates the two repo-wide audits use, so a
    scrub that swallowed genuine violations would fail here first.
    """
    webhook_url = "https://user:pass@hooks.example/hook?token=secret&pct=%20#frag"

    assert _has_foreign_placeholder(webhook_url) is False
    assert _has_unescaped_percent(webhook_url) is False

    embedded = (
        "UPDATE webhooks SET target = 'https://hooks.example/hook?token=a&pct=%20' "
        "WHERE id = ? AND ratio > 50%"
    )

    assert _has_foreign_placeholder(embedded) is True
    assert _has_unescaped_percent(embedded) is True
    assert _has_foreign_placeholder("SELECT * FROM tasks WHERE id = $1") is True


def test_prepare_exemption_requires_exactly_one_statement() -> None:
    assert _has_foreign_placeholder("PREPARE query(int) AS SELECT $1") is False
    assert _has_foreign_placeholder("PREPARE query(int) AS SELECT $1;") is False
    assert _has_foreign_placeholder("PREPARE query(int) AS SELECT $1; SELECT $2") is True


def _has_foreign_placeholder(sql: str) -> bool:
    scrubbed = _scrub_sql(sql)
    if "?" in scrubbed:
        return True
    if _is_single_prepare_statement(scrubbed):
        # A server-side PREPARE statement's $n markers are PostgreSQL's own
        # prepared-parameter syntax, not a foreign client placeholder style.
        return False
    return DOLLAR_PLACEHOLDER_RE.search(scrubbed) is not None


def _is_single_prepare_statement(scrubbed: str) -> bool:
    candidate = scrubbed.strip()
    if not re.match(r"PREPARE\b", candidate, re.IGNORECASE):
        return False
    body = candidate[:-1].rstrip() if candidate.endswith(";") else candidate
    return ";" not in body


def _has_unescaped_percent(sql: str) -> bool:
    return RAW_PERCENT_RE.search(_scrub_comments_and_dollar_quotes(sql)) is not None


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
    scrubbed = _scrub_comments_and_dollar_quotes(sql)
    scrubbed = JSONB_KEY_EXISTS_RE.sub(" ", scrubbed)
    return _scrub_quoted_strings(scrubbed)


def _scrub_comments_and_dollar_quotes(sql: str) -> str:
    return NON_PLACEHOLDER_RE.sub(" ", sql)


def _scrub_quoted_strings(sql: str) -> str:
    return re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", " ", sql)
