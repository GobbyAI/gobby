"""JavaScript and TypeScript test-quality analysis."""

from __future__ import annotations

import re
from collections.abc import Iterable

from gobby.test_quality._analyzer_common import (
    _TODO_RE,
    _append_issue,
    _script_suppressed_codes,
)
from gobby.test_quality._analyzer_scanner import (
    _find_matching_delimiter,
    _next_non_whitespace_index,
)
from gobby.test_quality.models import AuditIssue

# The lookbehind rejects member access such as `/^a/.test(...)` or prose like
# `foo.test.tsx (...)` in comments — test declarations are never dot-prefixed.
_SCRIPT_TEST_CALL_RE = re.compile(r"(?<![.\w$])(?P<name>it|test)(?P<modifier>\.\w+)?\s*\(")
_SCRIPT_ASSERTION_RE = re.compile(r"\b(?:expect|assert(?:\.\w+)?)\s*\(")
_SCRIPT_SLEEP_RE = re.compile(r"\b(?:setTimeout|setInterval)\s*\(")


def _analyze_script_file(source: str, relative_path: str) -> tuple[list[AuditIssue], int]:
    issues: list[AuditIssue] = []
    tests_scanned = 0

    for test_name, modifier, call_source, line in _iter_script_tests(source):
        tests_scanned += 1
        suppressions = _script_suppressed_codes(call_source)

        if modifier == ".skip":
            _append_issue(
                issues, relative_path, test_name, "UNCONDITIONAL_SKIP", line, suppressions
            )

        if _SCRIPT_SLEEP_RE.search(call_source):
            _append_issue(issues, relative_path, test_name, "SLEEP_IN_TEST", line, suppressions)

        if _TODO_RE.search(call_source) and "test-quality:" not in call_source:
            _append_issue(issues, relative_path, test_name, "TODO_IN_TEST", line, suppressions)

        if not _SCRIPT_ASSERTION_RE.search(call_source):
            _append_issue(issues, relative_path, test_name, "NO_ASSERTION", line, suppressions)

    return issues, tests_scanned


def _iter_script_tests(source: str) -> Iterable[tuple[str, str | None, str, int]]:
    offset = 0
    while match := _SCRIPT_TEST_CALL_RE.search(source, offset):
        open_paren = source.find("(", match.start())
        close_paren = _find_matching_delimiter(source, open_paren, "(", ")")
        if close_paren is None:
            offset = match.end()
            continue

        final_close_paren = close_paren
        if match.group("modifier") == ".each":
            next_open_paren = _next_non_whitespace_index(source, close_paren + 1)
            if next_open_paren is not None and source[next_open_paren] == "(":
                each_close_paren = _find_matching_delimiter(source, next_open_paren, "(", ")")
                if each_close_paren is not None:
                    final_close_paren = each_close_paren

        call_source = source[match.start() : final_close_paren + 1]
        test_name = _script_test_name(call_source) or f"{match.group('name')}_at_line"
        line = source.count("\n", 0, match.start()) + 1
        yield test_name, match.group("modifier"), call_source, line
        offset = final_close_paren + 1


def _script_test_name(call_source: str) -> str | None:
    open_paren = call_source.find("(")
    if open_paren == -1:
        return None
    index = _next_non_whitespace_index(call_source, open_paren + 1)
    if index is None:
        return None
    quote = call_source[index]
    if quote not in {"'", '"', "`"}:
        return None

    chars: list[str] = []
    escaped = False
    index += 1
    while index < len(call_source):
        char = call_source[index]
        if escaped:
            chars.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            return "".join(chars)
        else:
            chars.append(char)
        index += 1
    return None
