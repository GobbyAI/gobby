"""Attribute RED failures in rtk-compacted pytest output to the named tests."""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from pathlib import PurePosixPath

from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.tdd_python_evidence import _test_node_from_module

_COMPACTED_RECALL_RE = re.compile(r"(?m)^\[full output: rtk recall [0-9a-f]{12,64}\]$")
_PYTEST_FAILURE_HEADER_RE = re.compile(r"^_+\s+(?P<name>[^\s\[]+(?:\[[^\r\n]*\])?)\s+_+\s*$")
_RTK_FAILURE_HEADER_RE = re.compile(
    r"^\s*\d+\.\s+\[FAIL\]\s+(?P<name>[^\s\[]+(?:\[[^\r\n]*\])?)\s*$"
)
# pytest.fail() reached from a test body. pytest-timeout kills report as
# `Failed: Timeout`, which proves nothing about the code under test.
_PYTEST_FAIL_DETAIL_RE = re.compile(r"^\s*E\s+Failed:(?!\s+Timeout\b)", re.MULTILINE)
_PYTHON_EXCEPTION_DETAIL_RE = re.compile(
    r"^\s*E\s+(?:[A-Za-z_][A-Za-z0-9_.]*)(?:Error|Exception)(?::|\s*$)",
    re.MULTILINE,
)
_FAILED_NODE_RE = re.compile(r"FAILED (\S+\.py)::(\S+)(?:\s+-\s+.*)?")
_PYTEST_SUMMARY_RE = re.compile(r"(?m)^[= ]*(\d+) failed\b.*$")
_ERROR_COUNT_RE = re.compile(r"\b[1-9]\d* errors?\b")


def _path_matches_artifact(path: str, test: AcceptanceTest) -> bool:
    expected_parts = PurePosixPath(test.path).parts
    reported_parts = PurePosixPath(path).parts
    return reported_parts[-len(expected_parts) :] == expected_parts


def has_compacted_pytest_failure(
    output: str, test: AcceptanceTest, module: ast.Module | None
) -> bool:
    """Credit a compacted RED only when every FAILED node is provably bound to its body."""
    if not test.path.endswith(".py") or not _COMPACTED_RECALL_RE.search(output):
        return False
    lines = output.splitlines()
    # Pytest's final summary line is the last match; an earlier `N failed` line is text.
    summaries = list(_PYTEST_SUMMARY_RE.finditer(output))
    summary = summaries[-1] if summaries else None
    if (
        re.search(r"(?m)^ERROR\s|\[XPASS\(strict\)\]", output)
        or summary is None
        or _ERROR_COUNT_RE.search(summary[0])
    ):
        return False
    if any(
        _PYTEST_FAILURE_HEADER_RE.match(line) or _RTK_FAILURE_HEADER_RE.match(line)
        for line in lines
    ):
        return False  # Complete reports keep their existing section attribution.
    failed = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := _FAILED_NODE_RE.fullmatch(line))
    ]
    if not failed or int(summary[1]) != len(failed):
        return False
    expected = test.symbol.replace("::", ".")
    if len(failed) == 1:
        # One FAILED node: its primary detail is the only diagnostic it can own.
        index, match = failed[0]
        return (
            _path_matches_artifact(match[1], test)
            and _normalized(match[2], expected) == expected
            and any(_is_primary_detail(line) for line in lines[:index])
        )
    return _bound_failed_nodes(lines, failed, test, module)


def _normalized(reported: str, expected: str) -> str:
    # A parametrized id counts only when the named symbol carries the same id.
    name = reported.replace("::", ".")
    return name if "[" in expected else name.split("[", maxsplit=1)[0]


def _is_primary_detail(line: str) -> bool:
    return bool(
        re.match(r"^[ \t]*E[ \t]+assert(?:[ \t]|$)", line)
        or _PYTHON_EXCEPTION_DETAIL_RE.match(line)
        or _PYTEST_FAIL_DETAIL_RE.match(line)
    )


def _statement_key(text: str) -> str | None:
    try:
        tree = ast.parse(text.strip())
    except SyntaxError:
        return None
    return ast.unparse(tree.body[0]) if len(tree.body) == 1 else None


def _failure_statements(lines: list[str]) -> list[str] | None:
    """Return each `>` statement, provided its E lines open with a primary detail."""
    statements: list[str] = []
    for index, line in enumerate(lines):
        if not line.startswith(">"):
            continue
        details = []
        for row in lines[index + 1 :]:
            if not row.strip():
                continue
            if not re.match(r"^[ \t]*E(?:[ \t]|$)", row):
                break
            details.append(row)
        statement = _statement_key(line[1:])
        if statement is None or not details or not _is_primary_detail(details[0]):
            return None
        statements.append(statement)
    return statements


def _statement_keys(node: ast.AST) -> set[str]:
    return {
        ast.unparse(child)
        for child in ast.walk(node)
        if isinstance(child, ast.stmt) and child is not node
    }


def _bound_failed_nodes(
    lines: list[str],
    failed: list[tuple[int, re.Match[str]]],
    test: AcceptanceTest,
    module: ast.Module | None,
) -> bool:
    """Bind each body to the FAILED functions whose RED-time source holds its statement."""
    bodies = _failure_statements(lines[: failed[0][0]])
    if module is None or bodies is None or len(bodies) != len(failed):
        return False
    expected = test.symbol.replace("::", ".")
    functions: list[tuple[str, ast.AST]] = []
    for _, match in failed:
        if not _path_matches_artifact(match[1], test):
            return False
        base = match[2].replace("::", ".").split("[", maxsplit=1)[0]
        node = _test_node_from_module(replace(test, symbol=base), module)
        if node is None:
            return False
        functions.append((_normalized(match[2], expected), node))
    statements = {node: _statement_keys(node) for _, node in functions}
    bindings = [{node for node, keys in statements.items() if key in keys} for key in bodies]
    if any(not binding for binding in bindings) or not set(statements) <= set().union(*bindings):
        return False
    named = [node for reported, node in functions if reported == expected]
    return bool(named) and all(_exclusively_bound(node, bindings) for node in named)


def _exclusively_bound(node: ast.AST, bindings: list[set[ast.AST]]) -> bool:
    # A body shared with another FAILED function cannot prove this node's failure.
    own = [binding for binding in bindings if node in binding]
    return bool(own) and all(binding == {node} for binding in own)
