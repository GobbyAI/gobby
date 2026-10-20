"""Python acceptance-test bodies and the assertions gate 11 can prove in them.

An ``assert*``-named call proves nothing by its name. It counts only when the
call itself binds to one committed helper definition, in the caller's lexical
scope or a module it imports, whose own body asserts directly or through
another such call. Anything the resolver cannot bind exactly fails closed,
including a helper that anything other than an unshadowed ``staticmethod`` or
``classmethod`` decorates, and one the module rebinds anywhere: as an attribute,
through ``global``/``nonlocal``, or by a string literal a call passes or a
subscript indexes, where a dotted path counts by its last part. An attribute
call with no committed binding or write of its name, in its module, a ``conftest.py`` on
its directory chain, or a module one hop away that any of those imports, counts
only as a Mock assertion method or a ``unittest.TestCase`` instance assertion. Fakes
defined where the resolver cannot map an import to a repository file (plugins,
``pytest_plugins`` strings, installed packages, src-layout absolute imports) are
outside what it can see. An assertion counts only where it can run: statements after
``return``, ``raise``, ``continue`` or ``break`` in their block, and branches a
constant test rules out, are dead. A ``TestCase`` base reached through a local class
counts only when that class is bound once, undecorated, at module top level; a base
imported from another module stays a finding. Computed names, ``exec`` and metaclass
forms beyond literal keys are outside what the resolver can see.
"""

from __future__ import annotations

import ast
import operator
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
_STATEMENT_BLOCKS = ("body", "orelse", "finalbody", "handlers", "cases")
_TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


@dataclass(frozen=True, slots=True)
class _Module:
    path: str
    source: str
    tree: ast.Module
    #: Names the module writes as attributes, in global/nonlocal, or by string literal.
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
    for call in _live_scope_nodes(node.body):
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
    live = {id(node) for node in _live_scope_nodes(scope)}
    for node in _scope_nodes(scope):
        if isinstance(node, ast.Call):
            called = called_name(node.func)
            if called in {"assertEqual", "assertNotEqual", "assertTrue", "assertFalse"} and all(
                is_static_expression(arg) for arg in node.args
            ):
                findings.append(f"{reference}: assertion compares only constant values")
            if called in {"fail", "raises"} and id(node) in live:
                has_assertion = True
        if not isinstance(node, ast.Assert):
            continue
        if id(node) in live:
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
        if isinstance(target, ast.Attribute) and not await self._defined_nearby(
            caller.module, name
        ):
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
        for node in _live_scope_nodes(function.node.body):
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
        for candidate in _module_candidates(path, node.level, node.module):
            if candidate not in self._modules:
                self._modules[candidate] = await self._load(candidate)
            module = self._modules[candidate]
            if module is not None:
                loaded.append(module)
        # A module and a package of one name leave the binding ambiguous.
        return loaded[0] if len(loaded) == 1 else None

    async def _defined_nearby(self, module: _Module, name: str) -> bool:
        """Whether a scanned module defines or writes ``name``, or a module one hop away imports it.

        Scanned modules are the test module and every conftest.py on its directory chain.
        """
        for scanned in await self._scanned_modules(module):
            if _binds_nearby(scanned, name):
                return True
            for node in ast.walk(scanned.tree):
                targets: list[_Module] = []
                if isinstance(node, ast.ImportFrom):
                    targets = await self._loaded_candidates(scanned.path, node.level, node.module)
                    # ``from pkg import submodule`` binds the submodule, so it is checked too.
                    for alias in node.names:
                        if alias.name != "*":
                            dotted = f"{node.module}.{alias.name}" if node.module else alias.name
                            targets += await self._loaded_candidates(
                                scanned.path, node.level, dotted
                            )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        targets += await self._loaded_candidates(scanned.path, 0, alias.name)
                if any(_binds_nearby(target, name) for target in targets):
                    return True
        return False

    async def _scanned_modules(self, module: _Module) -> list[_Module]:
        """The test module, then each conftest.py from its directory up to the repository root."""
        scanned = [module]
        for directory in PurePosixPath(module.path).parents:
            loaded = await self._cached((directory / "conftest.py").as_posix())
            if loaded is not None:
                scanned.append(loaded)
        return scanned

    async def _loaded_candidates(self, path: str, level: int, name: str | None) -> list[_Module]:
        """Every candidate file an import names that loads: the module file and the package."""
        loaded: list[_Module] = []
        for candidate in _module_candidates(path, level, name):
            found = await self._cached(candidate)
            if found is not None:
                loaded.append(found)
        return loaded

    async def _cached(self, path: str) -> _Module | None:
        if path not in self._modules:
            self._modules[path] = await self._load(path)
        return self._modules[path]

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
    """Whether anything but an unshadowed staticmethod or classmethod wraps it or its class."""
    owner = function.owner.decorator_list if function.owner else []
    return any(
        not (
            isinstance(decorator, ast.Name)
            and decorator.id in {"staticmethod", "classmethod"}
            and not _shadowed(function.module, decorator.id)
        )
        for decorator in [*function.node.decorator_list, *owner]
    )


def _shadowed(module: _Module, name: str) -> bool:
    """Whether anything in ``module``, at any scope, binds or writes ``name``."""
    return name in module.written or any(
        bound in {name, "*"} for node in ast.walk(module.tree) for bound in _bound_names(node)
    )


def _written_names(tree: ast.Module) -> frozenset[str]:
    """Names anything in ``tree`` can rebind on an object or across scopes.

    Any string literal can reach a namespace (``globals().update([...])``,
    ``setattr``, ``patch.dict``, ``monkeypatch.setattr``), so the last dotted
    part of every literal counts as written when it is an identifier, whatever
    the prefix, as does every keyword name.
    """
    written: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            written.add(node.attr)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            written.update(node.names)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            written.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A dotted target such as "pkg.module.name" rebinds its last part,
            # whatever the prefix: monkeypatch imports "test_x-extra.name" too.
            name = node.value.rsplit(".", 1)[-1]
            if name.isidentifier():
                written.add(name)
    return frozenset(written)


def _binds_nearby(module: _Module, name: str) -> bool:
    """Whether a module binds ``name`` or writes it as an attribute, keyword or literal."""
    return _shadowed(module, name)


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


def _is_testcase(module: _Module, base: ast.expr, depth: int = 0) -> bool:
    """Whether a class base is unittest's TestCase, through imports or a local base class."""
    if depth >= MAX_HELPER_DEPTH:
        return False
    if isinstance(base, ast.Name):
        origins = _import_origins(module, base.id)
        if origins:
            return len(origins) == 1 and origins <= _TESTCASE_ORIGINS
        return _local_testcase(module, base.id, depth)
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


def _module_candidates(path: str, level: int, name: str | None) -> tuple[str, ...]:
    """Repository files an import can name; none when it climbs past the top package."""
    package = PurePosixPath(path).parent.parts
    if level > len(package):
        return ()
    base = package[: len(package) - level + 1] if level else ()
    parts = (*base, *(name.split(".") if name else ()))
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


def _live_scope_nodes(body: Iterable[ast.stmt], *, break_scope: bool = False) -> Iterator[ast.AST]:
    """Nodes a scope can run: ``_scope_nodes`` minus code no run reaches.

    Skipped: statements after a block-ending statement, branches a constant test rules out,
    short-circuit operands after the operand that decides the result, loop bodies over a
    statically empty iterable, and handlers of a try whose body returns a literal. Comprehension
    bodies are declined outright; only the first iterable is kept, so an unconsumed generator
    body never counts as executed. A break-scope walk excludes nested loops and breaks
    overridden by a terminating finally block.
    """
    queue: deque[ast.AST] = deque(_live_block(body))
    while queue:
        node = queue.popleft()
        yield node
        if isinstance(node, _DECLARATIONS):
            continue
        if break_scope and isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            # A nested loop's else belongs to the enclosing break scope; its body does not.
            if isinstance(node, ast.While):
                runs_else = _static_truth(node.test) is not True
            else:
                runs_else = not _raises_at_iterator(node.iter)
                nonempty = _is_literal(node.iter) and bool(ast.literal_eval(node.iter))
                if isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Name):
                    if node.iter.func.id == "range" and _literal_range(node.iter.args) is not None:
                        nonempty = not _is_empty_range(node.iter.args)
                if nonempty and _block_ends(node.body):
                    runs_else = any(
                        isinstance(child, ast.Continue)
                        for child in _live_scope_nodes(node.body, break_scope=True)
                    )
            if runs_else:
                queue.extend(_live_block(node.orelse))
            continue
        if isinstance(node, ast.If):
            truth = _static_truth(node.test)
            if truth is not None:
                queue.append(node.test)
                queue.extend(_live_block(node.body if truth else node.orelse))
                continue
        elif isinstance(node, ast.IfExp):
            truth = _static_truth(node.test)
            if truth is not None:
                queue.append(node.test)
                queue.append(node.body if truth else node.orelse)
                continue
        elif isinstance(node, ast.While) and _static_truth(node.test) is False:
            queue.append(node.test)
            queue.extend(_live_block(node.orelse))
            continue
        elif isinstance(node, ast.While) and _static_truth(node.test) is True:
            queue.append(node.test)
            queue.extend(_live_block(node.body))
            continue
        elif isinstance(node, ast.BoolOp):
            queue.extend(_live_operands(node))
            continue
        elif isinstance(node, ast.Compare):
            queue.append(node.left)
            for index, operand in enumerate(node.comparators, 1):
                queue.append(operand)
                prefix = ast.Compare(
                    left=node.left, ops=node.ops[:index], comparators=node.comparators[:index]
                )
                if _static_comparison(prefix) is False:
                    break
            continue
        elif isinstance(node, (ast.For, ast.AsyncFor)) and _raises_at_iterator(node.iter):
            # The iterator raises before any item, so neither the body nor the else clause runs.
            queue.append(node.iter)
            continue
        elif isinstance(node, (ast.For, ast.AsyncFor)) and _is_empty_literal(node.iter):
            queue.append(node.iter)
            queue.extend(_live_block(node.orelse))
            continue
        elif isinstance(node, (ast.Try, ast.TryStar)):
            if break_scope and _block_ends(node.finalbody):
                queue.extend(_live_block(node.finalbody))
                continue
            queue.extend(_live_block(node.body))
            # The else clause runs only when the body completes normally.
            if not _block_ends(node.body):
                queue.extend(_live_block(node.orelse))
            # A literal return cannot raise, so no handler runs.
            if not _returns_literal(node.body):
                queue.extend(node.handlers)
            queue.extend(_live_block(node.finalbody))
            continue
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            queue.append(node.generators[0].iter)
            continue
        queue.extend(_live_children(node))


def _live_block(statements: Iterable[ast.AST]) -> list[ast.AST]:
    """A block's statements up to and including the first one that ends the block."""
    live: list[ast.AST] = []
    for statement in statements:
        live.append(statement)
        if _ends_block(statement):
            break
    return live


def _ends_block(node: ast.AST) -> bool:
    """Whether control never reaches the statement after ``node`` in its block.

    A truthy-constant while without a reachable break ends the block around it. Other loops
    can run zero times, and break or continue ends only one iteration. A for over a literal
    zero-step range is another exception:
    its iterator raises before any item, so the block ends as it does at raise. A with statement
    ends it when its body ends by return, since a return itself is never suppressed. A body
    ending in raise does not, because a context manager can suppress that raise. A try whose body
    returns a literal, after only pass statements, ends the block too: nothing in that body can
    raise, so no handler runs.
    """
    if isinstance(node, _TERMINATORS):
        return True
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return _raises_at_iterator(node.iter)
    if isinstance(node, ast.While):
        return _static_truth(node.test) is True and not _breaks_loop(node.body)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return _returns_block(node.body)
    if isinstance(node, ast.If):
        truth = _static_truth(node.test)
        if truth is None:
            return _block_ends(node.body) and _block_ends(node.orelse)
        return _block_ends(node.body if truth else node.orelse)
    if isinstance(node, (ast.Try, ast.TryStar)):
        if _block_ends(node.finalbody):
            return True
        # A literal return cannot raise, so no handler runs and control leaves the block.
        if _returns_literal(node.body):
            return True
        normal_ends = _block_ends(node.body) or _block_ends(node.orelse)
        return normal_ends and all(_block_ends(handler.body) for handler in node.handlers)
    return False


def _breaks_loop(body: Iterable[ast.stmt]) -> bool:
    """Whether a reachable break leaves this loop, excluding nested loops and declarations."""
    return any(isinstance(node, ast.Break) for node in _live_scope_nodes(body, break_scope=True))


def _block_ends(statements: Iterable[ast.AST]) -> bool:
    return any(_ends_block(statement) for statement in statements)


def _returns_block(statements: Iterable[ast.AST]) -> bool:
    """Whether a block ends, and every path that ends it does so with a return."""
    live = _live_block(statements)
    return bool(live) and _returns_statement(live[-1])


def _returns_statement(node: ast.AST) -> bool:
    if isinstance(node, ast.Return):
        return True
    if isinstance(node, ast.If):
        truth = _static_truth(node.test)
        if truth is None:
            return _returns_block(node.body) and _returns_block(node.orelse)
        return _returns_block(node.body if truth else node.orelse)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return _returns_block(node.body)
    return False


def _returns_literal(statements: Iterable[ast.AST]) -> bool:
    """Whether a block is only pass statements ending in a return of a literal: nothing raises."""
    live = _live_block(statements)
    last = live[-1] if live else None
    if not isinstance(last, ast.Return):
        return False
    only_passes = all(isinstance(statement, ast.Pass) for statement in live[:-1])
    return only_passes and _is_literal(last.value)


def _is_literal(node: ast.expr | None) -> bool:
    """Whether a return value is a literal that builds without raising; bare return is None."""
    if node is None:
        return True
    if any(isinstance(child, ast.Call) for child in ast.walk(node)):
        return False
    try:
        ast.literal_eval(node)
    except (TypeError, ValueError):
        return False
    return True


def _live_operands(node: ast.BoolOp) -> list[ast.expr]:
    """Operands a boolean operator evaluates: up to the first one that decides its result."""
    decisive = isinstance(node.op, ast.Or)
    live: list[ast.expr] = []
    for operand in node.values:
        live.append(operand)
        if _static_truth(operand) is decisive:
            break
    return live


_EMPTY_BUILDERS = frozenset({"dict", "frozenset", "list", "set", "tuple"})


def _literal_range(args: list[ast.expr]) -> tuple[int, int, int] | None:
    """Start, stop and step of a ``range`` call whose arguments are all literal integers."""
    if not 1 <= len(args) <= 3:
        return None
    try:
        values = [ast.literal_eval(arg) for arg in args]
    except (TypeError, ValueError):
        return None
    if not all(isinstance(value, int) for value in values):
        return None
    if len(values) == 1:
        return 0, values[0], 1
    if len(values) == 2:
        return values[0], values[1], 1
    return values[0], values[1], values[2]


def _is_empty_range(args: list[ast.expr]) -> bool:
    """Whether ``range`` over literal integers yields nothing, decided by comparing bounds.

    Bounds are compared rather than measured, so a span wider than a machine word never
    overflows. A zero step is not empty: its iterator raises (see ``_raises_at_iterator``).
    """
    bounds = _literal_range(args)
    if bounds is None or bounds[2] == 0:
        return False
    start, stop, step = bounds
    return start >= stop if step > 0 else start <= stop


def _raises_at_iterator(node: ast.expr) -> bool:
    """Whether iterating ``node`` raises before any item: a literal ``range`` with step zero."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "range"
        and not node.keywords
    ):
        return False
    bounds = _literal_range(node.args)
    return bounds is not None and bounds[2] == 0


def _is_empty_literal(node: ast.expr) -> bool:
    """Whether a literal or builtin call yields no items, so a loop over it never runs its body."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        if node.func.id in _EMPTY_BUILDERS:
            return not node.args
        return node.func.id == "range" and _is_empty_range(node.args)
    return (
        isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)) and not node.value
    )


def _live_children(node: ast.AST) -> Iterator[ast.AST]:
    for field, value in ast.iter_fields(node):
        if field in _STATEMENT_BLOCKS and isinstance(value, list):
            yield from _live_block(value)
        elif isinstance(value, list):
            yield from (item for item in value if isinstance(item, ast.AST))
        elif isinstance(value, ast.AST):
            yield value


def _static_truth(node: ast.expr) -> bool | None:
    """Truth value of a constant test, or None when the test is not a constant."""
    if isinstance(node, ast.NamedExpr):
        return _static_truth(node.value)
    if isinstance(node, ast.IfExp):
        test = _static_truth(node.test)
        if test is not None:
            return _static_truth(node.body if test else node.orelse)
        body = _static_truth(node.body)
        return body if body is not None and body is _static_truth(node.orelse) else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _static_truth(node.operand)
        return None if inner is None else not inner
    if isinstance(node, ast.BoolOp):
        decisive = isinstance(node.op, ast.Or)
        unknown = False
        for operand in node.values:
            truth = _static_truth(operand)
            if truth is decisive:
                return truth
            unknown |= truth is None
        return None if unknown else not decisive
    if isinstance(node, ast.Compare):
        return _static_comparison(node)
    if _is_literal(node):
        return bool(ast.literal_eval(node))
    return None


def _static_comparison(node: ast.Compare) -> bool | None:
    """Compare only literal values, stopping before an unreachable chained operand."""
    operations = {
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
        ast.Lt: operator.lt,
        ast.LtE: operator.le,
        ast.Gt: operator.gt,
        ast.GtE: operator.ge,
    }
    previous = node.left
    unknown = False
    for op, following in zip(node.ops, node.comparators, strict=True):
        if isinstance(op, (ast.Is, ast.IsNot)) and any(
            _is_literal(operand)
            and not _literal_identity(ast.literal_eval(operand), ast.literal_eval(operand))
            for operand in (previous, following)
        ):
            if isinstance(op, ast.Is):
                return False
            previous = following
            continue
        if isinstance(op, (ast.In, ast.NotIn)) and _is_literal(following):
            container = ast.literal_eval(following)
            if isinstance(container, (tuple, list, dict, set)) and not container:
                if isinstance(op, ast.In):
                    return False
                previous = following
                continue
        if not _is_literal(previous) or not _is_literal(following):
            unknown = True
            previous = following
            continue
        left, right = ast.literal_eval(previous), ast.literal_eval(following)
        try:
            if isinstance(op, (ast.Is, ast.IsNot)):
                result = _literal_identity(left, right)
                if isinstance(op, ast.IsNot):
                    result = not result
            elif isinstance(op, (ast.In, ast.NotIn)):
                result = left in right
                if isinstance(op, ast.NotIn):
                    result = not result
            else:
                result = operations[type(op)](left, right)
        except (TypeError, ValueError):
            return None
        if not result:
            return False
        previous = following
    return None if unknown else True


def _literal_identity(left: object, right: object) -> bool:
    """CPython constant keys merge immutable literals; mutable displays stay distinct."""
    if type(left) is not type(right) or isinstance(left, (list, dict, set)):
        return False
    if isinstance(left, tuple) and isinstance(right, tuple):
        return len(left) == len(right) and all(
            _literal_identity(a, b) for a, b in zip(left, right, strict=True)
        )
    # repr preserves scalar type/sign distinctions, including positive and negative zero.
    return repr(left) == repr(right)


def _local_testcase(module: _Module, name: str, depth: int) -> bool:
    """Whether ``name`` is one undecorated class bound once at module top level, whose bases
    are TestCase."""
    if name in module.written:
        return False
    binders = _binders(_scope_nodes(module.tree.body), name)
    if len(binders) != 1 or not isinstance(binders[0], ast.ClassDef):
        return False
    definition = binders[0]
    if definition.decorator_list or not any(definition is node for node in module.tree.body):
        return False
    return any(_is_testcase(module, base, depth + 1) for base in definition.bases)


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
