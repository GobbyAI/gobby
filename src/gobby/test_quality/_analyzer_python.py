"""Python AST rules for test-quality analysis."""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from gobby.test_quality._analyzer_common import (
    _append_issue,
    _collect_comments,
    _Comment,
    _suppressed_codes,
    _todo_lines,
)
from gobby.test_quality.models import AuditIssue

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


@dataclass(frozen=True, slots=True)
class _TestNode:
    name: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    decorators: tuple[ast.expr, ...]
    start_line: int
    end_line: int


def _analyze_python_file(
    source: str, relative_path: str, *, filename: str
) -> tuple[list[AuditIssue], int]:
    tree = ast.parse(source, filename=filename)
    comments = _collect_comments(source)
    sleep_call_names = _sleep_call_names(tree.body)

    issues: list[AuditIssue] = []
    test_nodes = list(_iter_test_nodes(tree))
    for test in test_nodes:
        suppressions = _suppressed_codes(comments, test.start_line, test.end_line)
        issues.extend(_analyze_test(relative_path, test, comments, suppressions, sleep_call_names))
    return issues, len(test_nodes)


def _iter_test_nodes(tree: ast.Module) -> Iterable[_TestNode]:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            if _has_pytest_fixture_decorator(node.decorator_list):
                continue
            yield _make_test_node(node.name, node, ())
        elif isinstance(node, ast.ClassDef) and _is_test_class(node):
            yield from _iter_class_test_nodes(node, node.name, ())


def _iter_class_test_nodes(
    node: ast.ClassDef,
    name: str,
    inherited_decorators: tuple[ast.expr, ...],
) -> Iterable[_TestNode]:
    class_decorators = inherited_decorators + tuple(node.decorator_list)
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith(
            "test_"
        ):
            if _has_pytest_fixture_decorator(child.decorator_list):
                continue
            yield _make_test_node(f"{name}.{child.name}", child, class_decorators)
        elif isinstance(child, ast.ClassDef) and _is_test_class(child):
            yield from _iter_class_test_nodes(
                child,
                f"{name}.{child.name}",
                class_decorators,
            )


def _is_test_class(node: ast.ClassDef) -> bool:
    return node.name.startswith("Test") or any(
        _call_name(base).endswith("TestCase") for base in node.bases
    )


def _make_test_node(
    name: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    inherited_decorators: tuple[ast.expr, ...],
) -> _TestNode:
    decorators = inherited_decorators + tuple(node.decorator_list)
    decorator_lines = [
        decorator.lineno for decorator in node.decorator_list if hasattr(decorator, "lineno")
    ]
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
    module_sleep_call_names: set[str],
) -> list[AuditIssue]:
    local_nodes = (node for statement in test.node.body for node in ast.walk(statement))
    sleep_call_names = module_sleep_call_names | _sleep_call_names(local_nodes)
    facts = _TestBodyVisitor(sleep_call_names)
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
    def __init__(self, sleep_call_names: set[str]) -> None:
        self.strong_assertions = 0
        self.mock_assertions = 0
        self.mock_uses = 0
        self.truthy_assert_lines: list[int] = []
        self.sleep_lines: list[int] = []
        self.sleep_call_names = sleep_call_names

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

        if _is_sleep_call(node, call_name, self.sleep_call_names):
            self.sleep_lines.append(node.lineno)

        self.generic_visit(node)


def _is_truthy_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and bool(node.value) is True


def _is_unconditional_skip(decorator: ast.expr) -> bool:
    call = decorator.func if isinstance(decorator, ast.Call) else decorator
    name = _call_name(call)
    return name in {"skip", "pytest.mark.skip", "unittest.skip"}


def _is_xfail_without_strict_or_reason(decorator: ast.expr) -> bool:
    name = _call_name(decorator.func if isinstance(decorator, ast.Call) else decorator)
    if name not in {"xfail", "pytest.mark.xfail"}:
        return False
    if not isinstance(decorator, ast.Call):
        return True

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
    if leaf_name in {"raises", "warns", "deprecated_call"}:
        return True
    if call_name.startswith("self.assert") or call_name.startswith("cls.assert"):
        return True
    return leaf_name.startswith(("assert_", "_assert_")) and not _is_mock_assertion(call_name)


def _is_mock_assertion(call_name: str) -> bool:
    return call_name.rsplit(".", 1)[-1] in _MOCK_ASSERT_NAMES


def _sleep_call_names(nodes: Iterable[ast.AST]) -> set[str]:
    call_names = {"time.sleep", "asyncio.sleep"}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"time", "asyncio"}:
                    call_names.add(f"{alias.asname or alias.name}.sleep")
        elif isinstance(node, ast.ImportFrom) and node.module in {"time", "asyncio"}:
            for alias in node.names:
                if alias.name == "sleep":
                    call_names.add(alias.asname or alias.name)
    return call_names


def _is_zero_delay(node: ast.Call) -> bool:
    """A literal-zero delay is a cooperative yield, not sleep-based timing."""
    delay: ast.expr | None = node.args[0] if node.args else None
    if delay is None:
        for keyword in node.keywords:
            if keyword.arg == "delay" or keyword.arg == "secs":
                delay = keyword.value
                break
    if delay is None:
        return False
    return isinstance(delay, ast.Constant) and type(delay.value) in (int, float) and not delay.value


def _is_sleep_call(node: ast.Call, call_name: str, sleep_call_names: set[str]) -> bool:
    if call_name not in sleep_call_names:
        return False
    return not _is_zero_delay(node)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return ""
