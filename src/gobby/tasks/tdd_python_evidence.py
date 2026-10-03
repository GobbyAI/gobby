"""Python API shape and original-source proof for TDD evidence."""

from __future__ import annotations

import ast
import re
import textwrap

from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.tdd_paths import is_production_edit_path
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_tool_arguments import python_edit_tokens, python_noop_module


def _source_confirmed_before(edit: TranscriptEdit, run: TranscriptValidationRun) -> bool:
    return (
        edit.source_confirmed
        and edit.source_confirmed_at is not None
        and edit.source_confirmed_at < run.started_at
    )


def _original_test_module(
    test: AcceptanceTest, evidence: TranscriptEvidence, run: TranscriptValidationRun
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
    try:
        node = ast.parse(textwrap.dedent(source))
    except (SyntaxError, ValueError):
        if latest.source_after is not None:
            return None
        # Appended tests may follow the tail of the preceding function in an
        # Edit payload. Only complete module-level definitions carry body proof.
        start = re.search(r"(?m)^(?:(?:async )?def |class |@)", source)
        if start is None:
            return None
        try:
            node = ast.parse(source[start.start() :])
        except (SyntaxError, ValueError):
            return None
    return node


def _original_test_node(
    test: AcceptanceTest, evidence: TranscriptEvidence, run: TranscriptValidationRun
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    node: ast.AST | None = _original_test_module(test, evidence, run)
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


def _has_python_keyword_stub(
    test: AcceptanceTest, evidence: TranscriptEvidence, run: TranscriptValidationRun
) -> bool:
    node = _original_test_node(test, evidence, run)
    module = _original_test_module(test, evidence, run)
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
    test: AcceptanceTest, evidence: TranscriptEvidence, run: TranscriptValidationRun
) -> bool:
    node = _original_test_node(test, evidence, run)
    original_module = _original_test_module(test, evidence, run)
    if node is None or original_module is None:
        return False
    reachable = _reachable_python_nodes(original_module, node)
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
    for edit in latest_by_path.values():
        stub_source = edit.source_after if edit.source_created else edit.python_added_source
        if stub_source is None or not _source_confirmed_before(edit, run):
            continue
        classes = python_noop_module(stub_source, context_source=edit.source_after)
        if classes is None:
            continue
        module_name = edit.path.removeprefix("src/").removesuffix(".py").replace("/", ".")
        aliases = {
            alias.asname or alias.name: classes[alias.name]
            for statement in original_module.body
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
        for bridge in latest_by_path.values():
            if bridge.source_after is None or not _source_confirmed_before(bridge, run):
                continue
            bridge_name = bridge.path.removeprefix("src/").removesuffix(".py").replace("/", ".")
            imported = {
                alias.asname or alias.name: alias.name
                for statement in original_module.body
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
            try:
                bridge_module = ast.parse(bridge.source_after)
            except (SyntaxError, ValueError):
                continue
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
    test: AcceptanceTest, evidence: TranscriptEvidence, run: TranscriptValidationRun
) -> bool:
    """Prove the invoked module stayed identical from before test writing through RED.

    This credits a repeated write after a move already completed before the test.
    A changed body, binding or task-owned dependency invalidates the proof.
    """
    module = _original_test_module(test, evidence, run)
    node = _original_test_node(test, evidence, run)
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
    invoked_paths = {
        "src/" + aliases[name][0].replace(".", "/") + ".py"
        for name in called - shadowed
        if name in aliases
    }
    for alias in called - shadowed:
        binding = aliases.get(alias)
        if binding is None or _binding_count(module, alias) != 1:
            continue
        module_name, function_name = binding
        path = "src/" + module_name.replace(".", "/") + ".py"
        if any(
            edit.order > test_edit.order and edit.path in invoked_paths - {path} for edit in edits
        ):
            continue
        history = [edit for edit in edits if edit.path == path]
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
        try:
            product = ast.parse(baseline.source_after)
        except (SyntaxError, ValueError):
            continue
        functions = [
            item
            for item in product.body
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            and item.name == function_name
        ]
        if len(functions) != 1:
            continue
        if _binding_count(product, function_name) != 1:
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
            and edit.path != path
            and any(
                _module_dependency_matches(edit.path, dependency) for dependency in dependencies
            )
            for edit in edits
        ):
            continue
        return True
    return False


def _binding_count(module: ast.Module, name: str) -> int:
    """Reject ambiguous bindings conservatively, including nested rebinding."""
    count = 0
    for item in ast.walk(module):
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store) and item.id == name:
            count += 1
        elif isinstance(item, ast.arg) and item.arg == name:
            count += 1
        elif isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            count += item.name == name
        elif isinstance(item, ast.Import | ast.ImportFrom):
            count += sum(
                (
                    alias.asname
                    or (alias.name.split(".")[0] if isinstance(item, ast.Import) else alias.name)
                )
                == name
                for alias in item.names
            )
    return count


def _module_dependency_matches(path: str, dependency: str) -> bool:
    module = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return (
        module == dependency
        or module.startswith(dependency + ".")
        or dependency.startswith(module + ".")
    )
