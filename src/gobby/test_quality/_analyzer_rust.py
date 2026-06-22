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
from gobby.test_quality._analyzer_scanner import _find_matching_delimiter
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

    while line_index < len(lines):
        line_number = line_index + 1
        line = lines[line_index]
        stripped = line.strip()
        attr_offset_in_line = line.index("#[") if stripped.startswith("#[") else -1
        if attr_offset_in_line >= 0:
            attr_offset = line_offset + attr_offset_in_line
            parsed_attr = _rust_attr_at(source, attr_offset)
            if parsed_attr is not None:
                attr_text, attr_end = parsed_attr
                pending_attrs.append((attr_text, line_number, attr_offset))
                line_index, line_offset = _advance_rust_lines_through_offset(
                    lines,
                    line_index,
                    line_offset,
                    attr_end,
                )
                continue

        single_line_attr = _rust_attr_text(stripped)
        if single_line_attr is not None:
            pending_attrs.append((single_line_attr, line_number, line_offset + line.index("#[")))
            line_index += 1
            line_offset += len(line)
            continue

        if not stripped or stripped.startswith("//"):
            line_index += 1
            line_offset += len(line)
            continue

        fn_match = _RUST_FN_RE.search(line)
        if fn_match is not None and _rust_attrs_mark_test(tuple(item[0] for item in pending_attrs)):
            fn_offset = line_offset + fn_match.start()
            open_brace = source.find("{", line_offset + fn_match.end())
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
        line_offset += len(line)


def _advance_rust_lines_through_offset(
    lines: Sequence[str],
    line_index: int,
    line_offset: int,
    target_offset: int,
) -> tuple[int, int]:
    while line_index < len(lines):
        next_offset = line_offset + len(lines[line_index])
        line_index += 1
        line_offset = next_offset
        if target_offset < next_offset:
            break
    return line_index, line_offset


def _rust_attr_at(source: str, attr_offset: int) -> tuple[str, int] | None:
    if not source.startswith("#[", attr_offset):
        return None
    end = _find_matching_delimiter(source, attr_offset + 1, "[", "]")
    if end is None:
        return None
    return source[attr_offset + 2 : end].strip(), end


def _rust_attr_text(stripped_line: str) -> str | None:
    if not stripped_line.startswith("#["):
        return None
    end = stripped_line.rfind("]")
    if end == -1:
        return None
    return stripped_line[2:end].strip()


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
    return "?" in body


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
