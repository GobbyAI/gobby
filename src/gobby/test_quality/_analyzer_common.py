"""Shared helpers for test-quality analyzers."""

from __future__ import annotations

import io
import re
import tokenize
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from gobby.test_quality.models import ISSUE_DEFINITIONS, AuditIssue, AuditWarning

_SUPPRESSION_RE = re.compile(r"test-quality:\s*allow\s+(.+?)\s+--\s*(\S.*)")
_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX)\b", re.IGNORECASE)
_PYTHON_SUFFIX = ".py"
_SCRIPT_TEST_SUFFIXES = {".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}
_RUST_SUFFIX = ".rs"
_UNSUPPORTED_TEST_LANGUAGE_SUFFIXES = {
    ".c",
    ".cc",
    ".clj",
    ".cpp",
    ".cs",
    ".cxx",
    ".ex",
    ".exs",
    ".fs",
    ".fsx",
    ".go",
    ".java",
    ".kt",
    ".kts",
    ".php",
    ".rb",
    ".scala",
    ".swift",
}


@dataclass(frozen=True, slots=True)
class _Comment:
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class _DiscoveryResult:
    files: tuple[Path, ...]
    warnings: tuple[AuditWarning, ...]


def _append_issue(
    issues: list[AuditIssue],
    path: str,
    test_name: str,
    issue_code: str,
    line: int,
    suppressions: set[str],
) -> None:
    if issue_code in suppressions:
        return
    definition = ISSUE_DEFINITIONS[issue_code]
    issues.append(
        AuditIssue(
            path=path,
            test_name=test_name,
            issue_code=definition.code,
            severity=definition.severity,
            line=line,
            message=definition.message,
        )
    )


def _collect_comments(source: str) -> tuple[_Comment, ...]:
    comments: list[_Comment] = []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type == tokenize.COMMENT:
            comments.append(_Comment(line=token.start[0], text=token.string.lstrip("#").strip()))
    return tuple(comments)


def _suppressed_codes(comments: Sequence[_Comment], start_line: int, end_line: int) -> set[str]:
    codes: set[str] = set()
    for comment in comments:
        if not start_line <= comment.line <= end_line:
            continue
        match = _SUPPRESSION_RE.search(comment.text)
        if not match:
            continue
        reason = match.group(2).strip()
        if not reason:
            continue
        for code in re.split(r"[,\s]+", match.group(1).strip()):
            normalized = code.strip().upper()
            if normalized in ISSUE_DEFINITIONS:
                codes.add(normalized)
    return codes


def _script_suppressed_codes(source: str) -> set[str]:
    codes: set[str] = set()
    for match in _SUPPRESSION_RE.finditer(source):
        reason = match.group(2).strip()
        if not reason:
            continue
        for code in re.split(r"[,\s]+", match.group(1).strip()):
            normalized = code.strip().upper()
            if normalized in ISSUE_DEFINITIONS:
                codes.add(normalized)
    return codes


def _todo_lines(comments: Sequence[_Comment], start_line: int, end_line: int) -> list[int]:
    return [
        comment.line
        for comment in comments
        if start_line <= comment.line <= end_line
        and "test-quality:" not in comment.text
        and _TODO_RE.search(comment.text)
    ]


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _deduplicate_issues(issues: Iterable[AuditIssue]) -> list[AuditIssue]:
    by_fingerprint: dict[str, AuditIssue] = {}
    for issue in sorted(
        issues, key=lambda item: (item.path, item.test_name, item.issue_code, item.line)
    ):
        by_fingerprint.setdefault(issue.fingerprint, issue)
    return list(by_fingerprint.values())
