"""Python acceptance-test bodies and the assertions gate 11 can prove in them.

An ``assert*``-named call proves nothing by its name. It counts only when the
call itself binds to one committed helper definition, in the caller's lexical
scope or a module it imports, whose own body asserts directly or through
another such call. Anything the resolver cannot bind exactly fails closed,
including a helper something decorates or rebinds by name anywhere in the
module. An attribute call with no committed definition of its name counts only
as a Mock assertion method or a ``unittest.TestCase`` instance assertion.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

#: Helper hops followed from the test body before resolution gives up.
MAX_HELPER_DEPTH = 8

ReadSource = Callable[[str], Awaitable[str]]
_Def = ast.FunctionDef | ast.AsyncFunctionDef
# Declarations whose bodies run only when something else calls them.
_DECLARATIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_MOCK_ASSERTIONS = frozenset(
    {
        "assert_called",
        "assert_called_once",
        "assert_called_with",
        "assert_called_once_with",
        "assert_any_call",
        "assert_has_calls",
        "assert_not_called",
        "assert_awaited",
        "assert_awaited_once",
        "assert_awaited_with",
        "assert_awaited_once_with",
        "assert_any_await",
        "assert_has_awaits",
        "assert_not_awaited",
    }
)
_TESTCASE_ORIGINS = frozenset({"unittest.TestCase", "unittest.IsolatedAsyncioTestCase"})


@dataclass(frozen=True, slots=True)
class _Module:
    path: str
    source: str
    tree: ast.Module
    #: Names the module writes as attributes, by literal setattr, or in global/nonlocal.
    written: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Function:
    """A committed function, and the class whose body declares it for a method."""

    module: _Module
    node: _Def
    owner: ast.ClassDef | None


def extract_python_test_body(source: str, symbol: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RuntimeError("committed test file is not parseable Python") from exc
    matches = _matching_functions(tree, symbol)
    if len(matches) != 1:
        raise RuntimeError(f"expected one matching symbol, found {len(matches)}")
    return _definition_text(source, matches[0][0])


async def resolve_assertion_helpers(
    source: str, path: str, symbol: str, read: ReadSource
) -> tuple[tuple[str, ...], bool]:
    """Helper definitions the test's own calls reach, and whether one proves an assertion."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return (), False
    matches = _matching_functions(tree, symbol)
    if len(matches) != 1:
        return (), False
    node, parents = matches[0]
    owners = [parent for parent in parents if isinstance(parent, ast.ClassDef)]
    if len(owners) != len(parents):
        # Enclosing function scopes are not modeled, so nothing binds.
        return (), False
    module = _Module(path, source, tree, _written_names(tree))
    test = _Function(module, node, owners[-1] if owners else None)
    resolver = _HelperResolver(read, module)
    proven = False
    for call in _scope_nodes(node.body):
        if (
            isinstance(call, ast.Call)
            and _is_assert_name(called_name(call.func))
            and await resolver.qualifies(test, call.func, 0)
        ):
            proven = True
    return tuple(resolver.definitions), proven


def python_test_findings(reference: str, body: str, helper_asserts: bool) -> list[str]:
    try:
        tree = ast.parse(textwrap.dedent(body))
    except SyntaxError:
        return [f"{reference}: test body is not parseable Python"]
    scope: list[ast.stmt] = tree.body
    if len(tree.body) == 1 and isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        scope = tree.body[0].body
    findings: list[str] = []
    has_assertion = helper_asserts
    for node in _scope_nodes(scope):
        if isinstance(node, ast.Call):
            called = called_name(node.func)
            if called in {"assertEqual", "assertNotEqual", "assertTrue", "assertFalse"} and all(
                is_static_expression(arg) for arg in node.args
            ):
                findings.append(f"{reference}: assertion compares only constant values")
            if called in {"fail", "raises"}:
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
    def __init__(self, read: ReadSource, module: _Module) -> None:
        self._read = read
        self._root = module
        self._modules: dict[str, _Module | None] = {module.path: module}
        self._proven: set[tuple[str, int, int]] = set()
        self._visiting: set[tuple[str, int, int]] = set()
        self._seen: set[tuple[str, int, int]] = set()
        self.definitions: list[str] = []

    async def qualifies(self, caller: _Function, target: ast.expr, depth: int) -> bool:
        if depth >= MAX_HELPER_DEPTH:
            return False
        name = called_name(target)
        if name in self._root.written or name in caller.module.written:
            return False
        if isinstance(target, ast.Attribute) and not _defines(caller.module.tree, name):
            return _library_assertion(caller, target)
        helper = await self._resolve(caller, target)
        if helper is None or _decorated(helper):
            return False
        if any(
            helper.node.name in module.written
            for module in (self._root, caller.module, helper.module)
        ):
            return False
        key = (helper.module.path, helper.node.lineno, helper.node.col_offset)
        if key in self._proven:
            return True
        if key in self._visiting:
            # A cycle proves nothing by itself.
            return False
        if key not in self._seen:
            self._seen.add(key)
            self.definitions.append(_definition_text(helper.module.source, helper.node))
        self._visiting.add(key)
        try:
            proven = await self._asserts(helper, depth + 1)
        finally:
            self._visiting.discard(key)
        if proven:
            self._proven.add(key)
        return proven

    async def _asserts(self, function: _Function, depth: int) -> bool:
        for node in _scope_nodes(function.node.body):
            if isinstance(node, ast.Assert):
                if not is_tautological_assert(node.test) and not is_static_expression(node.test):
                    return True
            elif isinstance(node, ast.Call):
                name = called_name(node.func)
                if name in {"fail", "raises"}:
                    return True
                if _is_assert_name(name) and await self.qualifies(function, node.func, depth):
                    return True
        return False

    async def _resolve(self, caller: _Function, target: ast.expr) -> _Function | None:
        """The one definition a call expression binds to, or None when unproven."""
        if isinstance(target, ast.Name):
            if target.id in _local_names(caller.node):
                return None
            return await self._module_function(caller.module, target.id, follow_import=True)
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and caller.owner is not None
            and _receiver_is_instance(caller.node, target.value.id)
        ):
            return _method(caller.module, caller.owner, target.attr)
        return None

    async def _module_function(
        self, module: _Module, name: str, *, follow_import: bool
    ) -> _Function | None:
        binders = _binders(_scope_nodes(module.tree.body), name)
        if len(binders) != 1 or not any(binders[0] is node for node in module.tree.body):
            return None
        binder = binders[0]
        if isinstance(binder, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return _Function(module, binder, None) if _defined_once(module.tree, name) else None
        if not follow_import or not isinstance(binder, ast.ImportFrom):
            return None
        aliases = [alias for alias in binder.names if (alias.asname or alias.name) == name]
        if len(aliases) != 1:
            return None
        imported = await self._imported_module(module.path, binder)
        if imported is None:
            return None
        return await self._module_function(imported, aliases[0].name, follow_import=False)

    async def _imported_module(self, path: str, node: ast.ImportFrom) -> _Module | None:
        loaded: list[_Module] = []
        for candidate in _module_candidates(path, node):
            if candidate not in self._modules:
                self._modules[candidate] = await self._load(candidate)
            module = self._modules[candidate]
            if module is not None:
                loaded.append(module)
        # A module and a package of one name leave the binding ambiguous.
        return loaded[0] if len(loaded) == 1 else None

    async def _load(self, path: str) -> _Module | None:
        try:
            source = await self._read(path)
            tree = ast.parse(source)
            return _Module(path, source, tree, _written_names(tree))
        except (OSError, RuntimeError, SyntaxError, ValueError):
            return None


def _method(module: _Module, owner: ast.ClassDef, name: str) -> _Function | None:
    """The method ``owner`` declares as ``name``, when nothing else binds it."""
    binders = _binders(_scope_nodes(owner.body), name)
    if len(binders) != 1 or not _defined_once(module.tree, name):
        return None
    binder = binders[0]
    if isinstance(binder, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        binder is node for node in owner.body
    ):
        return _Function(module, binder, owner)
    return None


def _decorated(function: _Function) -> bool:
    """Whether a decorator other than staticmethod or classmethod wraps it or its class."""
    owner = function.owner.decorator_list if function.owner else []
    return any(
        not (isinstance(decorator, ast.Name) and decorator.id in {"staticmethod", "classmethod"})
        for decorator in [*function.node.decorator_list, *owner]
    )


def _written_names(tree: ast.Module) -> frozenset[str]:
    """Names anything in ``tree`` can rebind on an object or across scopes."""
    written: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            written.add(node.attr)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            written.update(node.names)
        elif isinstance(node, ast.Call) and called_name(node.func) in {
            "setattr",
            "delattr",
            "__setattr__",
        }:
            written.update(
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            )
    return frozenset(written)


def _defines(tree: ast.Module, name: str) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in ast.walk(tree)
    )


def _library_assertion(caller: _Function, target: ast.Attribute) -> bool:
    """Whether an attribute call with no committed definition is a library assertion."""
    if target.attr in _MOCK_ASSERTIONS:
        return True
    return (
        target.attr.startswith(("assert", "fail"))
        and isinstance(target.value, ast.Name)
        and caller.owner is not None
        and _receiver_is_instance(caller.node, target.value.id)
        and any(_is_testcase(caller.module, base) for base in caller.owner.bases)
    )


def _is_testcase(module: _Module, base: ast.expr) -> bool:
    """Whether a class base names unittest's TestCase through the module's own imports."""
    if isinstance(base, ast.Name):
        origins = _import_origins(module, base.id)
        return len(origins) == 1 and origins <= _TESTCASE_ORIGINS
    return (
        isinstance(base, ast.Attribute)
        and isinstance(base.value, ast.Name)
        and f"unittest.{base.attr}" in _TESTCASE_ORIGINS
        and _import_origins(module, base.value.id) == {"unittest"}
    )


def _import_origins(module: _Module, name: str) -> set[str]:
    """Dotted origins of ``name`` when only top-level absolute imports bind it, else empty."""
    if name in module.written:
        return set()
    origins: set[str] = set()
    for binder in _binders(_scope_nodes(module.tree.body), name):
        if not any(binder is node for node in module.tree.body):
            return set()
        if isinstance(binder, ast.Import):
            for alias in binder.names:
                if alias.asname == name:
                    origins.add(alias.name)
                elif alias.asname is None and alias.name.split(".")[0] == name:
                    origins.add(name)
        elif isinstance(binder, ast.ImportFrom) and binder.level == 0 and binder.module:
            for alias in binder.names:
                if alias.name == "*":
                    return set()
                if (alias.asname or alias.name) == name:
                    origins.add(f"{binder.module}.{alias.name}")
        else:
            return set()
    return origins


def _receiver_is_instance(function: _Def, receiver: str) -> bool:
    """Whether ``receiver`` is the method's first parameter and nothing rebinds it."""
    positional = [*function.args.posonlyargs, *function.args.args]
    return (
        bool(positional)
        and positional[0].arg == receiver
        and not any(
            isinstance(decorator, ast.Name) and decorator.id == "staticmethod"
            for decorator in function.decorator_list
        )
        and not _binders(_scope_nodes(function.body), receiver)
    )


def _module_candidates(path: str, node: ast.ImportFrom) -> tuple[str, ...]:
    """Repository files an import can name; none when it climbs past the top package."""
    package = PurePosixPath(path).parent.parts
    if node.level > len(package):
        return ()
    base = package[: len(package) - node.level + 1] if node.level else ()
    parts = (*base, *(node.module.split(".") if node.module else ()))
    if not parts or not all(part.isidentifier() for part in parts):
        return ()
    module = "/".join(parts)
    return (f"{module}.py", f"{module}/__init__.py")


def _local_names(function: _Def) -> set[str]:
    """Every name the function's own scope binds, parameters included."""
    arguments = function.args
    names = {
        argument.arg
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *(item for item in (arguments.vararg, arguments.kwarg) if item is not None),
        )
    }
    names.update(name for node in _scope_nodes(function.body) for name in _bound_names(node))
    return names


def _binders(nodes: Iterable[ast.AST], name: str) -> list[ast.AST]:
    """One entry per binding of ``name``; a star import can bind any name."""
    return [node for node in nodes for bound in _bound_names(node) if bound in {name, "*"}]


def _bound_names(node: ast.AST) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [alias.asname or alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
        return [node.id]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names)
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def _scope_nodes(body: Iterable[ast.stmt]) -> Iterator[ast.AST]:
    """Nodes a scope executes itself, without entering nested declarations."""
    queue: deque[ast.AST] = deque(body)
    while queue:
        node = queue.popleft()
        yield node
        if not isinstance(node, _DECLARATIONS):
            queue.extend(ast.iter_child_nodes(node))


def _defined_once(tree: ast.Module, name: str) -> bool:
    return (
        sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
            for node in ast.walk(tree)
        )
        == 1
    )


def _matching_functions(
    tree: ast.Module, symbol: str
) -> list[tuple[_Def, tuple[ast.ClassDef | _Def, ...]]]:
    requested = tuple(part for part in re.split(r"::|\.", symbol) if part)
    matches: list[tuple[_Def, tuple[ast.ClassDef | _Def, ...]]] = []

    def visit(nodes: list[ast.stmt], parents: tuple[ast.ClassDef | _Def, ...]) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                visit(node.body, (*parents, node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = tuple(parent.name for parent in (*parents, node))
                if requested and names[-len(requested) :] == requested:
                    matches.append((node, parents))
                visit(node.body, (*parents, node))

    visit(tree.body, ())
    return matches


def _definition_text(source: str, node: _Def) -> str:
    start_line = min(
        (decorator.lineno for decorator in node.decorator_list),
        default=node.lineno,
    )
    if node.end_lineno is None:
        raise RuntimeError("committed Python symbol has no source boundary")
    return "".join(source.splitlines(keepends=True)[start_line - 1 : node.end_lineno])


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
