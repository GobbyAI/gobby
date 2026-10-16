"""Python acceptance-test bodies and the assertions gate 11 can prove in them.

An ``assert*``-named call proves nothing by its name. It counts only when it
resolves to one parseable helper definition, in the test file or in a module
the file imports, whose body holds an executable assertion directly or through
another qualifying helper.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath

#: Helper hops followed from the test body before resolution gives up.
MAX_HELPER_DEPTH = 8

ReadSource = Callable[[str], Awaitable[str]]


def extract_python_test_body(source: str, symbol: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RuntimeError("committed test file is not parseable Python") from exc
    requested = tuple(part for part in re.split(r"::|\.", symbol) if part)
    matches: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def visit(nodes: list[ast.stmt], parents: tuple[str, ...]) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                visit(node.body, (*parents, node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = (*parents, node.name)
                if requested and qualified[-len(requested) :] == requested:
                    matches.append(node)
                visit(node.body, qualified)

    visit(tree.body, ())
    if len(matches) != 1:
        raise RuntimeError(f"expected one matching symbol, found {len(matches)}")
    node = matches[0]
    start_line = min(
        (decorator.lineno for decorator in node.decorator_list),
        default=node.lineno,
    )
    if node.end_lineno is None:
        raise RuntimeError("committed Python symbol has no source boundary")
    return "".join(source.splitlines(keepends=True)[start_line - 1 : node.end_lineno])


async def resolve_assertion_helpers(
    source: str, path: str, body: str, read: ReadSource
) -> tuple[tuple[str, ...], frozenset[str]]:
    """Resolved helper definitions, and the body's assert-named calls they prove."""
    resolver = _HelperResolver(read)
    proven = frozenset(
        [
            name
            for name in _assert_call_names(body)
            if await resolver.qualifies(path, source, name, 0)
        ]
    )
    return tuple(resolver.definitions), proven


def python_test_findings(reference: str, body: str, assertion_calls: frozenset[str]) -> list[str]:
    try:
        tree = ast.parse(textwrap.dedent(body))
    except SyntaxError:
        return [f"{reference}: test body is not parseable Python"]
    findings: list[str] = []
    has_assertion = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = called_name(node.func)
            if called in {"assertEqual", "assertNotEqual", "assertTrue", "assertFalse"} and all(
                is_static_expression(arg) for arg in node.args
            ):
                findings.append(f"{reference}: assertion compares only constant values")
            if called in {"fail", "raises"} or called in assertion_calls:
                has_assertion = True
        if not isinstance(node, ast.Assert):
            continue
        has_assertion = True
        if is_tautological_assert(node.test) or is_static_expression(node.test):
            findings.append(f"{reference}: contains a constant or tautological assertion")
    if not has_assertion:
        findings.append(f"{reference}: contains no executable assertion")
    return list(dict.fromkeys(findings))


class _HelperResolver:
    def __init__(self, read: ReadSource) -> None:
        self._read = read
        self._proven: set[tuple[str, str]] = set()
        self._visiting: set[tuple[str, str]] = set()
        self._seen: set[tuple[str, str]] = set()
        self.definitions: list[str] = []

    async def qualifies(self, path: str, source: str, name: str, depth: int) -> bool:
        if depth >= MAX_HELPER_DEPTH:
            return False
        located = await self._locate(path, source, name)
        if located is None:
            return False
        helper_path, helper_source, helper_name = located
        key = (helper_path, helper_name)
        if key in self._proven:
            return True
        if key in self._visiting:
            # A cycle proves nothing by itself.
            return False
        try:
            definition = extract_python_test_body(helper_source, helper_name)
            tree = ast.parse(textwrap.dedent(definition))
        except (RuntimeError, SyntaxError):
            return False
        if key not in self._seen:
            self._seen.add(key)
            self.definitions.append(definition)
        self._visiting.add(key)
        try:
            proven = await self._asserts(helper_path, helper_source, tree, depth + 1)
        finally:
            self._visiting.discard(key)
        if proven:
            self._proven.add(key)
        return proven

    async def _asserts(self, path: str, source: str, tree: ast.AST, depth: int) -> bool:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                if not is_tautological_assert(node.test) and not is_static_expression(node.test):
                    return True
            elif isinstance(node, ast.Call):
                name = called_name(node.func)
                if name in {"fail", "raises"}:
                    return True
                if _is_assert_name(name) and await self.qualifies(path, source, name, depth):
                    return True
        return False

    async def _locate(self, path: str, source: str, name: str) -> tuple[str, str, str] | None:
        """The module and definition name a call resolves to in ``path``'s scope."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        imported = _imported_names(tree, path).get(name)
        if imported is None:
            return path, source, name
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
            for node in ast.walk(tree)
        ):
            # Imported and defined under one name: the binding is ambiguous.
            return None
        module_paths, original = imported
        for module_path in module_paths:
            try:
                return module_path, await self._read(module_path), original
            except (OSError, RuntimeError):
                continue
        return None


def _imported_names(tree: ast.AST, path: str) -> dict[str, tuple[tuple[str, ...], str]]:
    """``from module import name as alias`` bindings, with the files that could hold them."""
    names: dict[str, tuple[tuple[str, ...], str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        base = PurePosixPath(path).parent
        if node.level:
            for _ in range(node.level - 1):
                base = base.parent
            module = base.joinpath(*(node.module or "").split(".")) if node.module else base
        else:
            if node.module is None:
                continue
            module = PurePosixPath(*node.module.split("."))
        candidates = (f"{module}.py", f"{module}/__init__.py")
        for alias in node.names:
            names[alias.asname or alias.name] = (candidates, alias.name)
    return names


def _assert_call_names(body: str) -> list[str]:
    try:
        tree = ast.parse(textwrap.dedent(body))
    except SyntaxError:
        return []
    names = (called_name(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call))
    return list(dict.fromkeys(name for name in names if _is_assert_name(name)))


def _is_assert_name(name: str) -> bool:
    return name.lstrip("_").startswith("assert")


def called_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def is_static_expression(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(is_static_expression(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and is_static_expression(key) and is_static_expression(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, ast.JoinedStr):
        return all(
            isinstance(item, ast.Constant)
            or isinstance(item, ast.FormattedValue)
            and is_static_expression(item.value)
            for item in node.values
        )
    if isinstance(node, ast.BinOp):
        return is_static_expression(node.left) and is_static_expression(node.right)
    if isinstance(node, ast.UnaryOp):
        return is_static_expression(node.operand)
    if isinstance(node, ast.Compare):
        return is_static_expression(node.left) and all(
            is_static_expression(item) for item in node.comparators
        )
    if isinstance(node, ast.Call) and called_name(node.func) in {"str", "repr", "format"}:
        return all(is_static_expression(arg) for arg in node.args)
    return False


def is_tautological_assert(node: ast.AST) -> bool:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.Or):
            return any(
                isinstance(item, ast.Constant) and item.value is True for item in node.values
            )
        if isinstance(node.op, ast.And):
            return any(
                isinstance(item, ast.Constant) and item.value is False for item in node.values
            )
    return False


__all__ = [
    "MAX_HELPER_DEPTH",
    "extract_python_test_body",
    "python_test_findings",
    "resolve_assertion_helpers",
]
