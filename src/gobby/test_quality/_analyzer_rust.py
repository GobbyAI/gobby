"""Rust test-quality analysis."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from gobby.test_quality._analyzer_common import (
    _TODO_RE,
    _append_issue,
    _script_suppressed_codes,
)
from gobby.test_quality._analyzer_scanner import (
    _find_matching_delimiter,
    _rust_char_literal_end,
    _rust_lifetime_end,
    _rust_raw_string_end,
)
from gobby.test_quality.models import AuditIssue

_RUST_FN_RE = re.compile(
    r"\b(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b"
)
_RUST_ASSERTION_RE = re.compile(
    r"\b(?:assert|assert_eq|assert_ne|matches|panic|prop_assert|prop_assert_eq|"
    r"prop_assert_ne|proptest|quickcheck|quickcheck_macros::quickcheck)!\s*\("
    r"|\b(?:insta::)?assert_[A-Za-z0-9_]+!\s*\("
    r"|\bQuickCheck::new\s*\("
)
_RUST_ASSERT_TRUE_RE = re.compile(r"\bassert!\s*\(\s*true\s*(?:,|\))")
_RUST_SLEEP_RE = re.compile(r"\b(?:(?:std::)?thread::|tokio::time::|async_std::task::)?sleep\s*\(")


@dataclass(frozen=True, slots=True)
class _RustTest:
    name: str
    source: str
    attrs: tuple[str, ...]
    signature: str
    body: str
    start_line: int
    line: int


def _analyze_rust_file(source: str, relative_path: str) -> tuple[list[AuditIssue], int]:
    issues: list[AuditIssue] = []
    test_nodes = list(_iter_rust_tests(source))

    for test in test_nodes:
        suppressions = _script_suppressed_codes(test.source)

        for match in _RUST_ASSERT_TRUE_RE.finditer(test.source):
            _append_issue(
                issues,
                relative_path,
                test.name,
                "ASSERT_TRUE",
                _line_for_offset(test.source, match.start(), test.start_line),
                suppressions,
            )

        for match in _RUST_SLEEP_RE.finditer(test.source):
            _append_issue(
                issues,
                relative_path,
                test.name,
                "SLEEP_IN_TEST",
                _line_for_offset(test.source, match.start(), test.start_line),
                suppressions,
            )

        for line in _rust_todo_lines(test.source, test.start_line):
            _append_issue(issues, relative_path, test.name, "TODO_IN_TEST", line, suppressions)

        if _rust_has_unconditional_ignore(test.attrs):
            _append_issue(
                issues,
                relative_path,
                test.name,
                "UNCONDITIONAL_SKIP",
                test.start_line,
                suppressions,
            )

        if not _rust_has_assertion_like_check(test):
            _append_issue(
                issues,
                relative_path,
                test.name,
                "NO_ASSERTION",
                test.line,
                suppressions,
            )

    return issues, len(test_nodes)


def _iter_rust_tests(source: str) -> Iterable[_RustTest]:
    pending_attrs: list[tuple[str, int, int]] = []
    lines = source.splitlines(keepends=True)
    line_offset = 0
    line_index = 0
    scan_offset = 0

    while line_index < len(lines):
        line_number = line_index + 1
        line = lines[line_index]
        line_end = line_offset + len(line)
        cursor = max(line_offset, scan_offset)

        while cursor < line_end and source[cursor] in " \t\r":
            cursor += 1

        if source.startswith("#[", cursor):
            attr_offset = cursor
            parsed_attr = _rust_attr_at(source, attr_offset)
            if parsed_attr is not None:
                attr_text, attr_end = parsed_attr
                pending_attrs.append((attr_text, line_number, attr_offset))
                while attr_end >= line_end and line_index + 1 < len(lines):
                    line_index += 1
                    line_offset = line_end
                    line = lines[line_index]
                    line_end = line_offset + len(line)
                cursor = attr_end + 1
                while cursor < line_end and source[cursor] in " \t\r":
                    cursor += 1
                if cursor < line_end and source[cursor] != "\n":
                    scan_offset = cursor
                    continue
                line_index += 1
                line_offset = line_end
                scan_offset = line_offset
                continue

        stripped = source[cursor:line_end].strip()
        if not stripped or stripped.startswith("//"):
            line_index += 1
            line_offset = line_end
            scan_offset = line_offset
            continue

        fn_match = _RUST_FN_RE.search(source, cursor, line_end)
        if fn_match is not None and _rust_attrs_mark_test(tuple(item[0] for item in pending_attrs)):
            fn_offset = fn_match.start()
            open_brace = source.find("{", fn_match.end())
            close_brace = (
                _find_matching_delimiter(source, open_brace, "{", "}") if open_brace != -1 else None
            )
            if close_brace is not None:
                start_offset = pending_attrs[0][2] if pending_attrs else fn_offset
                start_line = pending_attrs[0][1] if pending_attrs else line_number
                yield _RustTest(
                    name=fn_match.group("name"),
                    source=source[start_offset : close_brace + 1],
                    attrs=tuple(item[0] for item in pending_attrs),
                    signature=source[fn_offset:open_brace],
                    body=source[open_brace + 1 : close_brace],
                    start_line=start_line,
                    line=line_number,
                )

        pending_attrs = []
        line_index += 1
        line_offset = line_end
        scan_offset = line_offset


def _rust_attr_at(source: str, attr_offset: int) -> tuple[str, int] | None:
    if not source.startswith("#[", attr_offset):
        return None
    end = _find_matching_delimiter(source, attr_offset + 1, "[", "]")
    if end is None:
        return None
    return source[attr_offset + 2 : end].strip(), end


def _rust_attr_name(attr: str) -> str:
    return attr.split("(", 1)[0].split("=", 1)[0].strip()


def _rust_attrs_mark_test(attrs: Sequence[str]) -> bool:
    names = {_rust_attr_name(attr) for attr in attrs}
    return bool(
        names
        & {
            "test",
            "tokio::test",
            "rstest",
            "test_case",
            "quickcheck",
            "quickcheck_macros::quickcheck",
        }
    )


def _rust_has_unconditional_ignore(attrs: Sequence[str]) -> bool:
    return any(_rust_attr_name(attr) == "ignore" for attr in attrs)


def _rust_has_should_panic(attrs: Sequence[str]) -> bool:
    return any(_rust_attr_name(attr) == "should_panic" for attr in attrs)


def _rust_has_assertion_like_check(test: _RustTest) -> bool:
    if _rust_has_should_panic(test.attrs):
        return True
    property_attrs = {"quickcheck", "quickcheck_macros::quickcheck"}
    if any(_rust_attr_name(attr) in property_attrs for attr in test.attrs):
        return True
    if _RUST_ASSERTION_RE.search(test.body):
        return True
    return _rust_returns_result(test.signature) and _rust_uses_question_mark(test.body)


def _rust_returns_result(signature: str) -> bool:
    return "->" in signature and bool(
        re.search(r"\bResult\s*(?:<|$)|::Result\s*(?:<|$)", signature)
    )


def _rust_uses_question_mark(body: str) -> bool:
    delimiter_stack: list[tuple[str, int]] = []
    matching_openers: dict[int, int] = {}
    index = 0

    while index < len(body):
        if body.startswith("//", index):
            newline = body.find("\n", index + 2)
            index = len(body) if newline == -1 else newline + 1
            continue
        if body.startswith("/*", index):
            depth = 1
            index += 2
            while index < len(body) and depth:
                if body.startswith("/*", index):
                    depth += 1
                    index += 2
                elif body.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            continue

        raw_string_end = _rust_raw_string_end(body, index)
        if raw_string_end is not None:
            index = raw_string_end + 1
            continue
        if body[index] == "'":
            char_end = _rust_char_literal_end(body, index)
            if char_end is not None:
                index = char_end + 1
                continue
            lifetime_end = _rust_lifetime_end(body, index)
            if lifetime_end is not None:
                index = lifetime_end
                continue
        if body[index] == '"':
            index += 1
            while index < len(body):
                if body[index] == "\\":
                    index += 2
                elif body[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
            continue

        char = body[index]
        if char in "([{":
            delimiter_stack.append((char, index))
        elif char in ")]}" and delimiter_stack:
            _, opener = delimiter_stack.pop()
            matching_openers[index] = opener
        elif char == "?":
            following = index + 1
            while following < len(body) and body[following].isspace():
                following += 1
            next_char = body[following : following + 1]
            previous = index - 1
            while previous >= 0 and body[previous].isspace():
                previous -= 1
            repetition_close = previous
            if repetition_close not in matching_openers:
                repetition_close -= 1
                while repetition_close >= 0 and body[repetition_close].isspace():
                    repetition_close -= 1
            macro_repetition = (
                repetition_close in matching_openers
                and matching_openers[repetition_close] > 0
                and body[matching_openers[repetition_close] - 1] == "$"
            )
            if next_char != "?" and not next_char.isidentifier() and not macro_repetition:
                return True
        index += 1

    return False


def _rust_todo_lines(source: str, start_line: int) -> list[int]:
    lines: list[int] = []
    for offset, line in _iter_source_lines(source):
        if "test-quality:" in line or not _TODO_RE.search(line):
            continue
        lines.append(_line_for_offset(source, offset, start_line))
    return lines


def _iter_source_lines(source: str) -> Iterable[tuple[int, str]]:
    offset = 0
    for line in source.splitlines(keepends=True):
        yield offset, line
        offset += len(line)


def _line_for_offset(source: str, offset: int, start_line: int) -> int:
    return start_line + source.count("\n", 0, offset)
