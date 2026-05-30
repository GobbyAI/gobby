"""AST-based test-quality analyzer."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from gobby.test_quality.models import ISSUE_DEFINITIONS, AuditIssue, AuditReport, AuditWarning

_SUPPRESSION_RE = re.compile(r"test-quality:\s*allow\s+(.+?)\s+--\s*(\S.*)")
_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX)\b", re.IGNORECASE)
_MOCK_ASSERT_NAMES = {
    "assert_any_await",
    "assert_any_call",
    "assert_awaited",
    "assert_awaited_once",
    "assert_awaited_once_with",
    "assert_awaited_with",
    "assert_called",
    "assert_called_once",
    "assert_called_once_with",
    "assert_called_with",
    "assert_has_awaits",
    "assert_has_calls",
    "assert_not_awaited",
    "assert_not_called",
}
_MOCK_FACTORY_NAMES = {
    "AsyncMock",
    "MagicMock",
    "Mock",
    "NonCallableMagicMock",
    "NonCallableMock",
    "PropertyMock",
    "create_autospec",
    "mock_open",
    "patch",
}
_PYTHON_SUFFIX = ".py"
_SCRIPT_TEST_SUFFIXES = {".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}
_SCRIPT_TEST_CALL_RE = re.compile(r"\b(?P<name>it|test)(?P<modifier>\.\w+)?\s*\(")
_SCRIPT_ASSERTION_RE = re.compile(r"\b(?:expect|assert(?:\.\w+)?)\s*\(")
_SCRIPT_SLEEP_RE = re.compile(r"\b(?:setTimeout|setInterval)\s*\(")
_RUST_SUFFIX = ".rs"
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
class _TestNode:
    name: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    decorators: tuple[ast.expr, ...]
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class _DiscoveryResult:
    files: tuple[Path, ...]
    warnings: tuple[AuditWarning, ...]


@dataclass(frozen=True, slots=True)
class _RustTest:
    name: str
    source: str
    attrs: tuple[str, ...]
    signature: str
    body: str
    start_line: int
    line: int


def audit_paths(paths: Sequence[str | Path], *, root: str | Path | None = None) -> AuditReport:
    """Audit pytest-style tests under paths."""

    root_path = Path.cwd() if root is None else Path(root)
    root_path = root_path.resolve()
    requested_paths = tuple(str(path) for path in paths)
    discovery = _discover_files(paths, root=root_path)

    issues: list[AuditIssue] = []
    tests_scanned = 0
    for file_path in discovery.files:
        file_issues, file_test_count = analyze_file(file_path, root=root_path)
        issues.extend(file_issues)
        tests_scanned += file_test_count

    return AuditReport(
        root=str(root_path),
        paths=requested_paths,
        issues=tuple(_deduplicate_issues(issues)),
        files_scanned=len(discovery.files),
        tests_scanned=tests_scanned,
        warnings=discovery.warnings,
    )


def analyze_file(
    path: str | Path, *, root: str | Path | None = None
) -> tuple[list[AuditIssue], int]:
    """Audit one test file."""

    file_path = Path(path)
    root_path = Path.cwd() if root is None else Path(root)
    source = file_path.read_text(encoding="utf-8")
    relative_path = _relative_path(file_path, root_path)

    if file_path.suffix in _SCRIPT_TEST_SUFFIXES:
        return _analyze_script_file(source, relative_path)
    if file_path.suffix == _RUST_SUFFIX:
        return _analyze_rust_file(source, relative_path)

    tree = ast.parse(source, filename=str(file_path))
    comments = _collect_comments(source)

    issues: list[AuditIssue] = []
    test_nodes = list(_iter_test_nodes(tree))
    for test in test_nodes:
        suppressions = _suppressed_codes(comments, test.start_line, test.end_line)
        issues.extend(_analyze_test(relative_path, test, comments, suppressions))
    return issues, len(test_nodes)


def _discover_files(paths: Sequence[str | Path], *, root: Path) -> _DiscoveryResult:
    files: set[Path] = set()
    warnings: dict[str, AuditWarning] = {}
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        if path.is_file() and _is_analyzable_file(path):
            files.add(path.resolve())
            continue
        if path.is_file() and _is_unsupported_test_file(path):
            warning = _unsupported_language_warning(path.resolve(), root)
            warnings[warning.path or str(path)] = warning
            continue
        if path.is_dir():
            for candidate in path.rglob("*"):
                if _is_analyzable_file(candidate):
                    files.add(candidate.resolve())
                elif _is_unsupported_test_file(candidate):
                    warning = _unsupported_language_warning(candidate.resolve(), root)
                    warnings[warning.path or str(candidate)] = warning
    return _DiscoveryResult(
        files=tuple(sorted(files)),
        warnings=tuple(warnings[key] for key in sorted(warnings)),
    )


def _is_analyzable_file(path: Path) -> bool:
    parts = set(path.parts)
    if "__pycache__" in parts or ".venv" in parts or ".mypy_cache" in parts:
        return False
    if path.suffix == _PYTHON_SUFFIX:
        return True
    if path.suffix in _SCRIPT_TEST_SUFFIXES:
        return ".test." in path.name or ".spec." in path.name or "__tests__" in parts
    if path.suffix == _RUST_SUFFIX:
        return True
    return False


def _is_unsupported_test_file(path: Path) -> bool:
    if not path.is_file() or path.suffix not in _UNSUPPORTED_TEST_LANGUAGE_SUFFIXES:
        return False
    parts = set(path.parts)
    name = path.name.lower()
    stem = path.stem.lower()
    return (
        "tests" in parts
        or "__tests__" in parts
        or "test" in stem
        or "spec" in stem
        or name.endswith("_test.go")
        or name.endswith("test.java")
    )


def _unsupported_language_warning(path: Path, root: Path) -> AuditWarning:
    relative_path = _relative_path(path, root)
    return AuditWarning(
        code="UNSUPPORTED_LANGUAGE",
        path=relative_path,
        message=f"Unsupported test language for {relative_path}; audit attempted but unsupported",
    )


def _iter_test_nodes(tree: ast.Module) -> Iterable[_TestNode]:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            if _has_pytest_fixture_decorator(node.decorator_list):
                continue
            yield _make_test_node(node.name, node, ())
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            class_decorators = tuple(node.decorator_list)
            for child in node.body:
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and child.name.startswith("test_"):
                    if _has_pytest_fixture_decorator(child.decorator_list):
                        continue
                    yield _make_test_node(f"{node.name}.{child.name}", child, class_decorators)


def _make_test_node(
    name: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    inherited_decorators: tuple[ast.expr, ...],
) -> _TestNode:
    decorators = inherited_decorators + tuple(node.decorator_list)
    decorator_lines = [decorator.lineno for decorator in decorators if hasattr(decorator, "lineno")]
    start_line = min([node.lineno, *decorator_lines])
    return _TestNode(
        name=name,
        node=node,
        decorators=decorators,
        start_line=start_line,
        end_line=getattr(node, "end_lineno", node.lineno),
    )


def _analyze_test(
    path: str,
    test: _TestNode,
    comments: Sequence[_Comment],
    suppressions: set[str],
) -> list[AuditIssue]:
    facts = _TestBodyVisitor()
    for decorator in test.decorators:
        facts.visit(decorator)
    for statement in test.node.body:
        facts.visit(statement)

    issues: list[AuditIssue] = []
    for line in facts.truthy_assert_lines:
        _append_issue(issues, path, test.name, "ASSERT_TRUE", line, suppressions)

    for line in facts.sleep_lines:
        _append_issue(issues, path, test.name, "SLEEP_IN_TEST", line, suppressions)

    for line in _todo_lines(comments, test.start_line, test.end_line):
        _append_issue(issues, path, test.name, "TODO_IN_TEST", line, suppressions)

    for decorator in test.decorators:
        line = getattr(decorator, "lineno", test.node.lineno)
        if _is_unconditional_skip(decorator):
            _append_issue(issues, path, test.name, "UNCONDITIONAL_SKIP", line, suppressions)
        if _is_xfail_without_strict_or_reason(decorator):
            _append_issue(
                issues,
                path,
                test.name,
                "XFAIL_WITHOUT_STRICT_OR_REASON",
                line,
                suppressions,
            )

    total_assertions = facts.strong_assertions + facts.mock_assertions
    if total_assertions == 0:
        _append_issue(issues, path, test.name, "NO_ASSERTION", test.node.lineno, suppressions)
    elif facts.strong_assertions == 0 and facts.mock_assertions > 0:
        _append_issue(
            issues, path, test.name, "ONLY_MOCK_ASSERTIONS", test.node.lineno, suppressions
        )

    if facts.mock_uses >= 4 and facts.strong_assertions <= 1:
        _append_issue(
            issues, path, test.name, "HEAVY_MOCK_LOW_ASSERT", test.node.lineno, suppressions
        )

    return issues


class _TestBodyVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.strong_assertions = 0
        self.mock_assertions = 0
        self.mock_uses = 0
        self.truthy_assert_lines: list[int] = []
        self.sleep_lines: list[int] = []

    def visit_Assert(self, node: ast.Assert) -> None:
        if _is_truthy_constant(node.test):
            self.truthy_assert_lines.append(node.lineno)
            self.strong_assertions += 1
        else:
            self.strong_assertions += 1
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _call_name(node.func)
        leaf_name = call_name.rsplit(".", 1)[-1]

        if _is_mock_assertion(call_name):
            self.mock_assertions += 1
        elif _is_strong_assertion_call(call_name):
            self.strong_assertions += 1

        if leaf_name in _MOCK_FACTORY_NAMES or call_name in {
            "patch.object",
            "mock.patch.object",
            "unittest.mock.patch.object",
        }:
            self.mock_uses += 1
        if leaf_name == "setattr" and "monkeypatch" in call_name:
            self.mock_uses += 1

        if _is_sleep_call(call_name):
            self.sleep_lines.append(node.lineno)

        self.generic_visit(node)


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


def _todo_lines(comments: Sequence[_Comment], start_line: int, end_line: int) -> list[int]:
    return [
        comment.line
        for comment in comments
        if start_line <= comment.line <= end_line
        and "test-quality:" not in comment.text
        and _TODO_RE.search(comment.text)
    ]


def _is_truthy_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and bool(node.value) is True


def _is_unconditional_skip(decorator: ast.expr) -> bool:
    call = decorator.func if isinstance(decorator, ast.Call) else decorator
    name = _call_name(call)
    return name in {"skip", "pytest.mark.skip", "unittest.skip"}


def _is_xfail_without_strict_or_reason(decorator: ast.expr) -> bool:
    if not isinstance(decorator, ast.Call):
        return False
    name = _call_name(decorator.func)
    if name not in {"xfail", "pytest.mark.xfail"}:
        return False

    has_strict_true = any(
        keyword.arg == "strict"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in decorator.keywords
    )
    has_reason = any(_is_nonempty_string(arg) for arg in decorator.args)
    has_reason = has_reason or any(
        keyword.arg == "reason" and _is_nonempty_string(keyword.value)
        for keyword in decorator.keywords
    )
    return not (has_strict_true and has_reason)


def _has_pytest_fixture_decorator(decorators: Sequence[ast.expr]) -> bool:
    fixture_names = {"fixture", "pytest.fixture", "pytest_asyncio.fixture"}
    return any(_call_name(decorator) in fixture_names for decorator in decorators)


def _is_nonempty_string(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant) and isinstance(node.value, str) and bool(node.value.strip())
    )


def _is_strong_assertion_call(call_name: str) -> bool:
    leaf_name = call_name.rsplit(".", 1)[-1]
    if call_name in {"pytest.raises", "pytest.warns", "pytest.deprecated_call"}:
        return True
    if call_name.startswith("self.assert") or call_name.startswith("cls.assert"):
        return True
    return leaf_name.startswith(("assert_", "_assert_")) and not _is_mock_assertion(call_name)


def _is_mock_assertion(call_name: str) -> bool:
    return call_name.rsplit(".", 1)[-1] in _MOCK_ASSERT_NAMES


def _is_sleep_call(call_name: str) -> bool:
    return call_name in {"sleep", "time.sleep", "asyncio.sleep"} or call_name.endswith(".sleep")


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return ""


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
    line_offset = 0

    for line_number, line in enumerate(source.splitlines(keepends=True), start=1):
        stripped = line.strip()
        attr = _rust_attr_text(stripped)
        if attr is not None:
            pending_attrs.append((attr, line_number, line_offset + line.index("#[")))
            line_offset += len(line)
            continue

        if not stripped or stripped.startswith("//"):
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
        line_offset += len(line)


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


def _find_matching_delimiter(
    source: str, open_index: int, open_char: str, close_char: str
) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    index = open_index
    rust_body = open_char == "{" and close_char == "}"

    while index < len(source):
        char = source[index]

        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue

        if rust_body:
            raw_string_end = _rust_raw_string_end(source, index)
            if raw_string_end is not None:
                index = raw_string_end + 1
                continue

            if char == "'":
                char_literal_end = _rust_char_literal_end(source, index)
                if char_literal_end is not None:
                    index = char_literal_end + 1
                    continue

                lifetime_end = _rust_lifetime_end(source, index)
                if lifetime_end is not None:
                    index = lifetime_end
                    continue

        if char in {"'", '"', "`"}:
            quote = char
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index

        index += 1

    return None


def _rust_raw_string_end(source: str, index: int) -> int | None:
    if source[index] != "r":
        return None

    cursor = index + 1
    while cursor < len(source) and source[cursor] == "#":
        cursor += 1

    if cursor >= len(source) or source[cursor] != '"':
        return None

    hashes = cursor - index - 1
    terminator = '"' + ("#" * hashes)
    end = source.find(terminator, cursor + 1)
    if end == -1:
        return None
    return end + len(terminator) - 1


def _rust_char_literal_end(source: str, index: int) -> int | None:
    cursor = index + 1
    if cursor >= len(source):
        return None

    if source[cursor] == "\\":
        cursor += 1
        if cursor >= len(source):
            return None
        if source[cursor] == "u" and cursor + 1 < len(source) and source[cursor + 1] == "{":
            cursor = source.find("}", cursor + 2)
            if cursor == -1:
                return None
        cursor += 1
    else:
        if source[cursor] in {"\n", "\r", "'"}:
            return None
        cursor += 1

    if cursor < len(source) and source[cursor] == "'":
        return cursor
    return None


def _rust_lifetime_end(source: str, index: int) -> int | None:
    cursor = index + 1
    if cursor >= len(source) or not (source[cursor].isalpha() or source[cursor] == "_"):
        return None
    cursor += 1
    while cursor < len(source) and (source[cursor].isalnum() or source[cursor] == "_"):
        cursor += 1
    return cursor


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


def _next_non_whitespace_index(source: str, start: int) -> int | None:
    for index in range(start, len(source)):
        if not source[index].isspace():
            return index
    return None


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
