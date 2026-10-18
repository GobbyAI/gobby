"""Python API shape and original-source proof for TDD evidence."""

from __future__ import annotations

import ast
import builtins
import copy
import re
import sys
import textwrap
from collections.abc import Iterable
from pathlib import PurePosixPath

from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.tdd_paths import is_production_edit_path
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_tool_arguments import (
    PythonModuleCache,
    parse_python_module,
    python_edit_tokens,
    python_noop_module,
)

PythonBindingCache = dict[ast.Module, dict[str, int]]


def _executed_python_nodes(statement: ast.AST) -> Iterable[ast.AST]:
    """Visit eager evaluations before their call or conditional boundary."""
    children: Iterable[ast.AST]
    if isinstance(statement, ast.Assign):
        children = [statement.value, *statement.targets]
    elif isinstance(statement, ast.AnnAssign):
        # Local annotations are not evaluated, even with no assigned value.
        children = [*(() if statement.value is None else (statement.value,)), statement.target]
    elif isinstance(statement, ast.AugAssign):
        children = [statement.target, statement.value]
    elif isinstance(statement, ast.Dict):
        children = (
            child
            for key, value in zip(statement.keys, statement.values, strict=True)
            for child in (key, value)
            if child is not None
        )
    elif isinstance(statement, ast.Call):
        # Starred positional arguments run before keywords, even when written later.
        children = [statement.func, *statement.args, *(kw.value for kw in statement.keywords)]
    elif isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        decorators = getattr(statement, "decorator_list", [])
        children = [
            *decorators,
            *statement.args.defaults,
            *(value for value in statement.args.kw_defaults if value is not None),
            *_decorator_applications(decorators),
        ]
    elif isinstance(statement, ast.ClassDef):
        children = [
            *statement.decorator_list,
            *statement.bases,
            *(kw.value for kw in statement.keywords),
            *statement.body,
            *_decorator_applications(statement.decorator_list),
        ]
    elif isinstance(statement, ast.Compare) and len(statement.comparators) > 1:
        # Later comparators run only when every earlier comparison holds.
        children = [statement.left, statement.comparators[0]]
    elif isinstance(statement, ast.TypeAlias):
        children = ()
    elif isinstance(statement, ast.If | ast.While | ast.IfExp | ast.Assert):
        children = [statement.test]
    elif isinstance(statement, ast.BoolOp):
        children = statement.values[:1]
    elif isinstance(statement, ast.For | ast.AsyncFor | ast.comprehension):
        children = [statement.iter]
    elif isinstance(statement, ast.With | ast.AsyncWith):
        children = [item.context_expr for item in statement.items]
    elif isinstance(statement, ast.Match):
        children = [statement.subject]
    elif isinstance(statement, ast.Try | ast.TryStar):
        children = ()
    elif isinstance(statement, ast.GeneratorExp):
        # Construction evaluates only the first iterator; the body stays lazy.
        children = [statement.generators[0].iter]
    elif isinstance(statement, ast.ListComp | ast.SetComp | ast.DictComp):
        # The remaining clauses and body are conditional on the first iterator.
        children = statement.generators[:1]
    else:
        children = ast.iter_child_nodes(statement)
    for child in children:
        yield from _executed_python_nodes(child)
    yield statement


def _decorator_applications(decorators: list[ast.expr]) -> list[ast.Call]:
    """Model each decorator's call on the defined object, innermost first."""
    return [ast.Call(func=decorator, args=[], keywords=[]) for decorator in reversed(decorators)]


def _has_python_test_tightening(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    red: TranscriptValidationRun,
    green: TranscriptValidationRun | None,
    parse_cache: PythonModuleCache | None = None,
) -> bool:
    """Prove a changed test node failed against the previously passing production tree."""
    if not test.path.endswith(".py") or green is None or green.completed_at is None:
        return False
    if any(
        edit.path != test.path
        and (
            green.order < edit.order < red.order
            or green.started_at <= edit.timestamp < red.started_at
        )
        for edit in evidence.edits
    ):
        return False
    edits = [
        edit
        for edit in evidence.edits
        if edit.session_id == red.session_id
        and edit.path == test.path
        and green.order < edit.order < red.order
        and green.completed_at < edit.timestamp < red.started_at
    ]
    latest = max(edits, key=lambda edit: edit.order, default=None)
    if latest is None or latest.source_after is None:
        return False
    baseline_edit = max(
        (
            edit
            for edit in evidence.edits
            if edit.session_id == green.session_id
            and edit.path == test.path
            and edit.order < green.order
            and edit.timestamp < green.started_at
        ),
        key=lambda edit: edit.order,
        default=None,
    )
    # Fragments cannot establish that a node was absent from the baseline module.
    if baseline_edit is None or baseline_edit.source_after is None:
        return False
    baseline = _original_test_module(test, evidence, green, parse_cache)
    current_module = _original_test_module(test, evidence, red, parse_cache)
    if baseline is None or current_module is None:
        return False
    before = copy.deepcopy(baseline)
    after = copy.deepcopy(current_module)
    current = _test_node_from_module(test, after)
    previous = _test_node_from_module(test, before)
    # A new node has no previously executed behavior to compare. Assertions may
    # themselves mutate bindings, so deleting that node cannot prove tightening.
    if current is None or previous is None:
        return False
    if not _normalize_expectations(current) or not _normalize_expectations(previous):
        return False
    return ast.dump(baseline) != ast.dump(current_module) and ast.dump(before) == ast.dump(after)


def _normalize_expectations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Mask literal expectations, retaining application calls and all executable setup."""
    assertions = [statement for statement in node.body if isinstance(statement, ast.Assert)]
    if not assertions:
        return False
    for statement in assertions:
        actual = statement.test
        if isinstance(actual, ast.Compare):
            if len(actual.comparators) != 1:
                return False
            try:
                ast.literal_eval(actual.comparators[0])
            except (ValueError, TypeError, SyntaxError):
                return False
            actual = actual.left
        if any(
            isinstance(child, ast.NamedExpr | ast.Lambda | ast.Await | ast.Yield | ast.YieldFrom)
            for child in ast.walk(actual)
        ) or isinstance(actual, ast.Constant):
            return False
        if statement.msg is not None:
            try:
                ast.literal_eval(statement.msg)
            except (ValueError, TypeError, SyntaxError):
                return False
        statement.test = actual
        statement.msg = None
    return True


def _retains_python_red_source(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    red: TranscriptValidationRun,
    green: TranscriptValidationRun,
    parse_cache: PythonModuleCache | None = None,
) -> bool:
    before = _original_test_module(test, evidence, red, parse_cache)
    after = _original_test_module(test, evidence, green, parse_cache)
    original_node = _original_test_node(test, evidence, red, parse_cache)
    committed = parse_python_module(textwrap.dedent(test.body), parse_cache)
    committed_node = _test_node_from_module(test, committed)
    if committed_node is None and committed is not None:
        # Acceptance artifacts for methods contain the method source without its class.
        matches = [
            node
            for node in committed.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == test.symbol.replace("::", ".").split(".")[-1]
        ]
        committed_node = matches[0] if len(matches) == 1 else None
    if (
        before is None
        or after is None
        or original_node is None
        or committed_node is None
        or ast.dump(before) != ast.dump(after)
        or ast.dump(original_node) != ast.dump(committed_node)
    ):
        return False
    if any(
        edit.path != test.path
        and not is_production_edit_path(edit.path)
        and red.order < edit.order < green.order
        for edit in evidence.edits
    ):
        return False
    for edit in evidence.edits:
        if edit.path != test.path or not red.order < edit.order < green.order:
            continue
        if edit.source_after is None or not _source_confirmed_before(edit, green):
            return False
        snapshot = parse_python_module(textwrap.dedent(edit.source_after), parse_cache)
        if snapshot is None or ast.dump(snapshot) != ast.dump(before):
            return False
    return True


def _source_confirmed_before(edit: TranscriptEdit, run: TranscriptValidationRun) -> bool:
    return (
        edit.source_confirmed
        and edit.source_confirmed_at is not None
        and edit.source_confirmed_at < run.started_at
    )


def _original_test_module(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    parse_cache: PythonModuleCache | None = None,
) -> ast.Module | None:
    edits = [
        edit
        for edit in evidence.edits
        if edit.session_id == run.session_id
        and edit.path == test.path
        and edit.timestamp < run.started_at
        and edit.order < run.order
    ]
    latest = max(edits, key=lambda edit: edit.order, default=None)
    if latest is None or not _source_confirmed_before(latest, run):
        return None
    source = latest.source_after or latest.source_fragment
    if source is None:
        return None
    node = parse_python_module(textwrap.dedent(source), parse_cache)
    if node is None:
        if latest.source_after is not None:
            return None
        # Appended tests may follow the tail of the preceding function in an
        # Edit payload. Only complete module-level definitions carry body proof.
        start = re.search(r"(?m)^(?:(?:async )?def |class |@)", source)
        if start is None:
            return None
        node = parse_python_module(source[start.start() :], parse_cache)
    return node


def _original_test_node(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    parse_cache: PythonModuleCache | None = None,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    return _test_node_from_module(test, _original_test_module(test, evidence, run, parse_cache))


def _test_node_from_module(
    test: AcceptanceTest,
    module: ast.Module | None,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    node: ast.AST | None = module
    for name in test.symbol.replace("::", ".").split("."):
        body = getattr(node, "body", ())
        matches = [child for child in body if getattr(child, "name", None) == name]
        if len(matches) != 1:
            return None
        node = matches[0]
    return node if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) else None


def _reachable_python_nodes(module: ast.Module, node: ast.AST) -> tuple[ast.AST, ...]:
    """Follow only helpers and globals referenced by the original named test."""
    bindings: dict[str, ast.AST] = {}
    for statement in module.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            bindings[statement.name] = statement
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = statement.value
    pending = [node]
    visited: set[str] = set()
    result: list[ast.AST] = []
    while pending:
        for item in ast.walk(pending.pop()):
            result.append(item)
            if isinstance(item, ast.Name) and item.id in bindings and item.id not in visited:
                visited.add(item.id)
                pending.append(bindings[item.id])
    return tuple(result)


def _has_python_runtime_stub(
    section: str,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    parse_cache: PythonModuleCache | None = None,
) -> bool:
    """Bind a raising frame to the latest confirmed pre-RED source and function."""
    latest = {
        PurePosixPath(edit.path).parts: edit
        for edit in sorted(evidence.edits, key=lambda item: item.order)
        if edit.session_id == run.session_id
        and edit.order < run.order
        and edit.timestamp < run.started_at
        and is_production_edit_path(edit.path)
    }
    rows = section.splitlines()
    for row, source in zip(rows, rows[1:], strict=False):
        frame = re.match(r"\s*(.+\.py):(\d+): in (\w+)\s*$", row)
        if frame is None or not re.match(r"\s*raise NotImplementedError\b", source):
            continue
        reported = PurePosixPath(frame[1]).parts
        for path, edit in latest.items():
            if reported[-len(path) :] != path or not edit.task_attributed:
                continue
            if not _source_confirmed_before(edit, run):
                continue
            historical_source = edit.source_after
            if historical_source is None:
                historical_source = textwrap.dedent(edit.source_fragment or "")
            module = parse_python_module(historical_source, parse_cache)
            if module is None:
                continue
            functions = [
                node
                for node in ast.walk(module)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == frame[3]
            ]
            if len(functions) != 1:
                continue
            raised = functions[0].body[0]
            if not isinstance(raised, ast.Raise):
                continue
            exception = raised.exc.func if isinstance(raised.exc, ast.Call) else raised.exc
            if (
                isinstance(exception, ast.Name)
                and exception.id == "NotImplementedError"
                and (edit.source_after is None or raised.lineno == int(frame[2]))
            ):
                return True
    return False


def _has_python_keyword_stub(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    parse_cache: PythonModuleCache | None = None,
) -> bool:
    node = _original_test_node(test, evidence, run, parse_cache)
    module = _original_test_module(test, evidence, run, parse_cache)
    if node is None or module is None:
        return False
    edits = sorted(
        (
            edit
            for edit in evidence.edits
            if edit.session_id == run.session_id
            and edit.timestamp < run.started_at
            and edit.order < run.order
            and is_production_edit_path(edit.path)
        ),
        key=lambda edit: edit.order,
    )
    for edit in edits:
        if not _source_confirmed_before(edit, run) or edit.python_stub is None:
            continue
        if any(
            later.path == edit.path
            and later.order > edit.order
            and not later.source_unchanged
            and (later.python_stub is None or not _source_confirmed_before(later, run))
            for later in edits
        ):
            continue
        name, keywords = edit.python_stub
        for call in _reachable_python_nodes(module, node):
            if not isinstance(call, ast.Call):
                continue
            called = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
            if called == name and any(keyword.arg in keywords for keyword in call.keywords):
                return True
    return False


def _has_python_module_stub(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    parse_cache: PythonModuleCache | None = None,
) -> bool:
    node = _original_test_node(test, evidence, run, parse_cache)
    original_module = _original_test_module(test, evidence, run, parse_cache)
    if node is None or original_module is None:
        return False
    reachable = _reachable_python_nodes(original_module, node)
    statements = (*original_module.body, *node.body)
    latest_by_path: dict[str, TranscriptEdit] = {}
    for edit in sorted(evidence.edits, key=lambda item: item.order):
        if (
            edit.session_id == run.session_id
            and edit.timestamp < run.started_at
            and edit.order < run.order
            and is_production_edit_path(edit.path)
            and not edit.source_unchanged
        ):
            latest_by_path[edit.path] = edit
    bridges: list[tuple[ast.Module, set[str]]] = []
    for bridge in latest_by_path.values():
        if bridge.source_after is None or not _source_confirmed_before(bridge, run):
            continue
        bridge_name = _python_module_name(bridge.path)
        imported = {
            alias.asname or alias.name: alias.name
            for statement in statements
            if isinstance(statement, ast.ImportFrom) and statement.module == bridge_name
            for alias in statement.names
        }
        invoked = {
            imported[item.func.id]
            for item in reachable
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id in imported
        }
        if not invoked:
            continue
        bridge_module = parse_python_module(bridge.source_after, parse_cache)
        if bridge_module is not None:
            bridges.append((bridge_module, invoked))
    for edit in latest_by_path.values():
        stub_source = edit.source_after if edit.source_created else edit.python_added_source
        if stub_source is None or not _source_confirmed_before(edit, run):
            continue
        classes = python_noop_module(
            stub_source, context_source=edit.source_after, parse_cache=parse_cache
        )
        if classes is None:
            continue
        stub_module = parse_python_module(stub_source, parse_cache)
        if stub_module is None:
            continue
        if edit.source_after is None and any(
            isinstance(statement, ast.ClassDef) and statement.bases
            for statement in stub_module.body
        ):
            # An inserted exception fragment alone cannot rule out existing uses.
            continue
        module_name = _python_module_name(edit.path)
        aliases = {
            alias.asname or alias.name: classes[alias.name]
            for statement in statements
            if isinstance(statement, ast.ImportFrom) and statement.module == module_name
            for alias in statement.names
            if alias.name in classes
        }
        if any(
            isinstance(item, ast.Name) and item.id in aliases and aliases[item.id] is None
            for item in reachable
        ):
            return True
        constructed = {
            call.func.id
            for call in reachable
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in aliases
        }
        members = set().union(*(aliases[name] or frozenset() for name in constructed))
        if any(
            isinstance(item, ast.Attribute)
            and item.attr in members
            or isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id in constructed
            for item in reachable
        ):
            return True
        for bridge_module, invoked in bridges:
            bridge_aliases = {
                alias.asname or alias.name
                for statement in bridge_module.body
                if isinstance(statement, ast.ImportFrom) and statement.module == module_name
                for alias in statement.names
                if alias.name in classes
            }
            for declaration in bridge_module.body:
                if not isinstance(declaration, ast.ClassDef) or declaration.name not in invoked:
                    continue
                constructors = [
                    member
                    for member in declaration.body
                    if isinstance(member, ast.FunctionDef) and member.name == "__init__"
                ]
                if any(
                    isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Name)
                    and item.func.id in bridge_aliases
                    for constructor in constructors
                    for item in ast.walk(constructor)
                ):
                    return True
    return False


def _has_python_unchanged_api(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    parse_cache: PythonModuleCache | None = None,
    binding_cache: PythonBindingCache | None = None,
) -> bool:
    """Prove the invoked module stayed identical from before test writing through RED.

    This credits a repeated write after a move already completed before the test.
    A changed body, binding or task-owned dependency invalidates the proof.
    """
    module = _original_test_module(test, evidence, run, parse_cache)
    node = _original_test_node(test, evidence, run, parse_cache)
    if module is None or node is None:
        return False
    edits = sorted(
        (
            edit
            for edit in evidence.edits
            if edit.session_id == run.session_id
            and edit.order < run.order
            and edit.timestamp < run.started_at
        ),
        key=lambda edit: edit.order,
    )
    test_edit = max(
        (edit for edit in edits if edit.path == test.path),
        key=lambda edit: edit.order,
        default=None,
    )
    if test_edit is None:
        return False
    reachable = _reachable_python_nodes(module, node)
    called = {
        item.func.id
        for item in reachable
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
    }
    aliases = {
        alias.asname or alias.name: (statement.module, alias.name)
        for statement in module.body
        if isinstance(statement, ast.ImportFrom) and statement.module and not statement.level
        for alias in statement.names
        if alias.name != "*"
    }
    shadowed = {
        item.id
        for item in reachable
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
    } | {item.arg for item in reachable if isinstance(item, ast.arg)}
    invoked_modules = {aliases[name][0] for name in called - shadowed if name in aliases}
    for alias in called - shadowed:
        binding = aliases.get(alias)
        if binding is None or _binding_count(module, alias, binding_cache) != 1:
            continue
        module_name, function_name = binding
        if any(
            edit.order > test_edit.order
            and _python_module_name(edit.path) in invoked_modules - {module_name}
            for edit in edits
        ):
            continue
        history = [edit for edit in edits if _python_module_name(edit.path) == module_name]
        baseline = max(
            (edit for edit in history if edit.order < test_edit.order),
            key=lambda edit: edit.order,
            default=None,
        )
        if baseline is None or baseline.source_after is None:
            continue
        tokens = python_edit_tokens(baseline.source_after)
        snapshots = [edit for edit in history if edit.order >= baseline.order]
        if not tokens or any(
            not _source_confirmed_before(edit, run)
            or edit.source_after is None
            or python_edit_tokens(edit.source_after) != tokens
            for edit in snapshots
        ):
            continue
        product = parse_python_module(baseline.source_after, parse_cache)
        if product is None:
            continue
        functions = [
            item
            for item in product.body
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            and item.name == function_name
        ]
        if len(functions) != 1:
            continue
        if _binding_count(product, function_name, binding_cache) != 1:
            continue
        if any(isinstance(item, ast.ImportFrom) and item.level for item in product.body):
            continue
        dependencies = {
            item.module for item in product.body if isinstance(item, ast.ImportFrom) and item.module
        } | {
            alias.name
            for item in product.body
            if isinstance(item, ast.Import)
            for alias in item.names
        }
        if any(
            edit.order > baseline.order
            and is_production_edit_path(edit.path)
            and _python_module_name(edit.path) != module_name
            and any(
                _module_dependency_matches(edit.path, dependency) for dependency in dependencies
            )
            for edit in edits
        ):
            continue
        return True
    return False


def _binding_count(module: ast.Module, name: str, cache: PythonBindingCache | None = None) -> int:
    """Reject ambiguous bindings conservatively, including nested rebinding."""
    if cache is not None and module in cache:
        return cache[module].get(name, 0)
    counts: dict[str, int] = {}
    for item in ast.walk(module):
        bindings: tuple[str, ...] = ()
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
            bindings = (item.id,)
        elif isinstance(item, ast.arg):
            bindings = (item.arg,)
        elif isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bindings = (item.name,)
        elif isinstance(item, ast.Import | ast.ImportFrom):
            bindings = tuple(
                alias.asname
                or (alias.name.split(".")[0] if isinstance(item, ast.Import) else alias.name)
                for alias in item.names
            )
        for binding in bindings:
            counts[binding] = counts.get(binding, 0) + 1
    if cache is not None:
        cache[module] = counts
    return counts.get(name, 0)


def _python_module_name(path: str) -> str:
    """Return the import name of a source path; a package's ``__init__`` is the package."""
    return path.removeprefix("src/").removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def _module_dependency_matches(path: str, dependency: str) -> bool:
    module = _python_module_name(path)
    return (
        module == dependency
        or module.startswith(dependency + ".")
        or dependency.startswith(module + ".")
    )


def _has_pytest_fail_placeholder(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    parse_cache: PythonModuleCache | None = None,
) -> bool:
    """Reject fail reached before control flow or a call into application code."""
    node = _original_test_node(test, evidence, run, parse_cache)
    if node is None:
        return False
    # Builtins, stdlib and test-framework setup are not calls into code under test.
    setup_roots = {"pytest", "unittest", "builtins"}
    fail_calls = {"pytest.fail"}
    module = _original_test_module(test, evidence, run, parse_cache)
    for item in [*(module.body if module is not None else ()), *node.body]:
        if isinstance(item, ast.Import):
            fail_calls.update(
                f"{alias.asname or alias.name}.fail"
                for alias in item.names
                if alias.name == "pytest"
            )
            setup_roots.update(
                alias.asname or alias.name.split(".")[0]
                for alias in item.names
                if alias.name.split(".")[0] in sys.stdlib_module_names | {"pytest"}
            )
        elif isinstance(item, ast.ImportFrom) and (item.module or "").split(".")[0] in (
            sys.stdlib_module_names | {"pytest"}
        ):
            setup_roots.update(alias.asname or alias.name for alias in item.names)
            if item.module == "pytest":
                fail_calls.update(
                    alias.asname or alias.name for alias in item.names if alias.name == "fail"
                )
    for statement in node.body:
        for item in _executed_python_nodes(statement):
            if isinstance(
                item,
                ast.If
                | ast.IfExp
                | ast.BoolOp
                | ast.For
                | ast.AsyncFor
                | ast.While
                | ast.Try
                | ast.TryStar
                | ast.With
                | ast.AsyncWith
                | ast.Match
                | ast.Assert
                | ast.comprehension,
            ) or (isinstance(item, ast.Compare) and len(item.comparators) > 1):
                return False
            if isinstance(item, ast.Call):
                if ast.unparse(item.func) in fail_calls:
                    return True
                root = ast.unparse(item.func).split(".", 1)[0]
                if root not in setup_roots and root not in vars(builtins):
                    return False
    return False
