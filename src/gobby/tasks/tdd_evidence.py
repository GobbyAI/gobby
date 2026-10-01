"""Deterministic red/green evidence checks for named acceptance tests."""

from __future__ import annotations

import ast
import builtins
import re
import shlex
import sys
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from gobby.tasks.acceptance_artifacts import (
    AcceptanceTest,
    is_assertion_failure,
    validation_run_covers_test,
    validation_run_names_test,
)
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_tool_arguments import python_noop_module

_ASSERTION_DETAIL_RE = re.compile(
    r"AssertionError|assertion failed|\bassert\b|panicked at|Failed:\s+DID NOT RAISE",
    re.IGNORECASE,
)
_PYTEST_FAILURE_HEADER_RE = re.compile(r"^_{2,}\s+(?P<name>\S+)\s+_{2,}\s*$")
_PYTEST_LOCATION_RE = re.compile(
    r"^\s*(?P<path>\S+\.py):(?P<line>\d+):"
    r"(?: in (?P<symbol>\S+)| (?:[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)|Failed)(?::.*)?"
    r"| assert(?:\s+.*)?)?\s*$"
)
# pytest.fail() reached from a test body. pytest-timeout kills report as
# `Failed: Timeout`, which proves nothing about the code under test.
_PYTEST_FAIL_DETAIL_RE = re.compile(r"^\s*E\s+Failed:(?!\s+Timeout\b)", re.MULTILINE)
_PYTHON_EXCEPTION_DETAIL_RE = re.compile(
    r"^\s*E\s+(?:[A-Za-z_][A-Za-z0-9_.]*)(?:Error|Exception)(?::|\s*$)",
    re.MULTILINE,
)
_RAISE_EXCEPTION_DETAIL_RE = re.compile(
    r"^[ \t]*>?[ \t]*raise[ \t]+[A-Za-z_][\w.]*(?:Error|Exception)\b",
    re.MULTILINE,
)
_PASS_STATUS_RE = re.compile(r"\b(?:PASSED|SKIPPED|XFAIL|XPASS)\b", re.IGNORECASE)
_FAILURE_SECTION_BOUNDARY_RE = re.compile(
    r"^(?:_{2,}\s+\S.*\s+_{2,}|(?:FAILED|ERROR|PASSED|SKIPPED)\s+\S.*|"
    r".*::\S+\s+(?:PASSED|FAILED|ERROR|SKIPPED)\b)",
    re.IGNORECASE,
)
_NON_EXECUTION_TEST_MATCHERS = frozenset({"gobby-test-quality-audit"})


def is_test_convention_path(path: str) -> bool:
    """A test module in any language or any file under a test directory."""
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    if (name == "tests.rs" or name.endswith("_tests.rs")) and any(
        part.casefold() == "src" for part in pure.parts[:-1]
    ):
        # Rust module tests: <module>/tests.rs or <module>_tests.rs under src/.
        return True
    return (
        any(part.casefold() in {"test", "tests", "__tests__"} for part in pure.parts[:-1])
        or name.startswith("test_")
        or "_test." in name
        or ".test." in name
        or ".spec." in name
    )


@dataclass(frozen=True, slots=True)
class TddEvidenceResult:
    """TDD evidence outcome for a close attempt."""

    passed: bool
    skipped: bool
    findings: tuple[str, ...]
    red_runs: tuple[str, ...] = ()
    green_runs: tuple[str, ...] = ()

    def details(self) -> dict[str, object]:
        return {
            "findings": list(self.findings),
            "red_runs": list(self.red_runs),
            "green_runs": list(self.green_runs),
        }


_DOCUMENTATION_ROOTS = frozenset({"docs", ".gobby"})
_INSTRUCTION_FILES = frozenset({"agents.md", "claude.md", "readme.md", "changelog.md"})
TDD_SKILL = "test-driven-development"
TDD_REQUIRED_LABEL = "tdd:required"
_TDD_EVIDENCE_PHRASE = "tdd evidence"
_TDD_CYCLE_KEYWORDS = frozenset({"red", "green", "refactor"})
_TDD_FAILING_TEST_PHRASE = "failing test"
_TDD_BEFORE_IMPLEMENTATION_PHRASE = "before implementation"


def task_requires_tdd(
    *,
    labels: Iterable[str],
    additional_skills: Iterable[str],
    validation_criteria: str | None,
    enforce_tdd: bool = False,
) -> bool:
    """Return whether task metadata requires transcript-backed red/green evidence."""
    if enforce_tdd or TDD_REQUIRED_LABEL in labels or TDD_SKILL in additional_skills:
        return True
    if not validation_criteria:
        return False
    lowered = validation_criteria.lower()
    if TDD_SKILL in lowered or _TDD_EVIDENCE_PHRASE in lowered:
        return True
    if all(_contains_word(lowered, keyword) for keyword in _TDD_CYCLE_KEYWORDS):
        return True
    return _TDD_FAILING_TEST_PHRASE in lowered and _TDD_BEFORE_IMPLEMENTATION_PHRASE in lowered


def _is_production_edit_path(path: str) -> bool:
    """Implementation edits only: neither test convention, docs, nor repo instructions."""
    if is_test_convention_path(path):
        return False
    pure = PurePosixPath(path)
    if pure.parts and pure.parts[0].casefold() in _DOCUMENTATION_ROOTS:
        return False
    return pure.name.casefold() not in _INSTRUCTION_FILES


def _contains_word(value: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", value) is not None


def evaluate_tdd_evidence(
    tests: tuple[AcceptanceTest, ...],
    evidence: TranscriptEvidence,
    *,
    renamed_test_paths: Mapping[str, tuple[str, ...]] | None = None,
) -> TddEvidenceResult:
    """Require one assertion-backed cycle and later coverage of every named test."""
    if not tests:
        return TddEvidenceResult(
            False, False, ("TDD is required but no named test reference resolved.",)
        )

    findings: list[str] = []
    cycle: tuple[TranscriptValidationRun, TranscriptEdit] | None = None
    for test in tests:
        test_paths = (test.path, *(renamed_test_paths or {}).get(test.path, ()))
        test_edits = sorted(
            (edit for edit in evidence.edits if edit.path in test_paths),
            key=lambda edit: edit.order,
        )
        if not test_edits:
            findings.append(f"{test.reference}: transcript has no edit of the named test")
            continue
        red = None
        green = None
        red_rejection = None
        production_edit_seen = False
        for test_edit in test_edits:
            red_test = (
                test
                if test_edit.path == test.path
                else replace(
                    test,
                    path=test_edit.path,
                    reference=f"{test_edit.path}::{test.symbol}",
                )
            )
            production_edits = sorted(
                (
                    edit
                    for edit in evidence.edits
                    if edit.order > test_edit.order and _is_production_edit_path(edit.path)
                ),
                key=lambda edit: edit.order,
            )
            if not production_edits:
                continue
            production_edit_seen = True
            production_edit = production_edits[0]
            window_red, window_rejection = _find_red_run(
                red_test, evidence, test_edit.order, production_edit
            )
            red_rejection = window_rejection or red_rejection
            if window_red is None:
                for later_production_edit in production_edits[1:]:
                    window_red, window_rejection = _find_red_run(
                        red_test,
                        evidence,
                        production_edit.order,
                        later_production_edit,
                        require_not_implemented=not test.path.endswith(".rs"),
                    )
                    red_rejection = window_rejection or red_rejection
                    if window_red is not None:
                        production_edit = later_production_edit
                        break
            if window_red is None:
                continue
            if red is None:
                red = window_red
            green = _find_green_run(
                test,
                evidence,
                window_red,
                after_order=production_edit.order,
            )
            if green is not None:
                red = window_red
                cycle = (red, production_edit)
                break
        if cycle is not None:
            break
        if not production_edit_seen:
            findings.append(f"{test.reference}: no production edit follows the test edit")
            continue
        if red is None:
            finding = (
                f"{test.reference}: missing assertion or panic failure after the test edit "
                "and before an implementation edit"
            )
            if red_rejection is not None:
                finding = f"{finding}; {red_rejection}"
            findings.append(finding)
            continue
        if green is None:
            findings.append(
                f"{test.reference}: assertion-backed red has no later production edit and pass"
            )

    if cycle is None:
        return TddEvidenceResult(False, False, tuple(findings))

    red, production_edit = cycle
    findings = []
    green_commands: list[str] = []
    for test in tests:
        green = _find_green_run(
            test,
            evidence,
            red,
            after_order=production_edit.order,
        )
        if green is None:
            findings.append(
                f"{test.reference}: no pass after the production edit that completed "
                "the task-level TDD cycle"
            )
            continue
        green_commands.append(green.command)

    return TddEvidenceResult(
        passed=not findings,
        skipped=False,
        findings=tuple(findings),
        red_runs=(red.command,),
        green_runs=tuple(green_commands),
    )


def _find_red_run(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    test_edit_order: int,
    first_non_test_edit: TranscriptEdit | None,
    *,
    require_not_implemented: bool = False,
) -> tuple[TranscriptValidationRun | None, str | None]:
    rejection = None
    for run in sorted(evidence.validation_runs, key=lambda item: item.order):
        if (
            run.outcome != "failure"
            or not _is_test_execution_run(run)
            or run.order <= test_edit_order
        ):
            continue
        if first_non_test_edit is not None and run.order >= first_non_test_edit.order:
            continue
        core_command = run.core_command
        if core_command is None:
            continue
        source_failure = _has_original_source_failure(test, evidence, run)
        if not source_failure and not validation_run_names_test(core_command, run.output, test):
            continue
        matched, reason = _has_named_red_failure(core_command, run.output, test)
        matched = matched or source_failure
        if matched:
            if _has_pytest_fail_placeholder(test, evidence, run):
                reason = "test body is an unconditional pytest.fail placeholder"
            elif not require_not_implemented or (
                _has_python_keyword_stub(test, evidence, run)
                or _has_python_module_stub(test, evidence, run)
            ):
                return run, None
            else:
                reason = (
                    "post-production red has no attributable NotImplementedError or proven API stub"
                )
        rejection = f"run {run.command!r} rejected: {reason}"
    return None, rejection


def _has_pytest_fail_placeholder(
    test: AcceptanceTest, evidence: TranscriptEvidence, run: TranscriptValidationRun
) -> bool:
    """Reject fail reached before control flow or a call into application code."""
    node = _original_test_node(test, evidence, run)
    if node is None:
        return False
    # Builtins, stdlib and test-framework setup are not calls into code under test.
    setup_roots = {"pytest", "unittest", "builtins"}
    fail_calls = {"pytest.fail"}
    module = _original_test_module(test, evidence, run)
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
            ):
                return False
            if isinstance(item, ast.Call):
                if ast.unparse(item.func) in fail_calls:
                    return True
                root = ast.unparse(item.func).split(".", 1)[0]
                if root not in setup_roots and root not in vars(builtins):
                    return False
    return False


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
        children = [
            *getattr(statement, "decorator_list", ()),
            *statement.args.defaults,
            *(value for value in statement.args.kw_defaults if value is not None),
        ]
    elif isinstance(statement, ast.ClassDef):
        children = [
            *statement.decorator_list,
            *statement.bases,
            *(kw.value for kw in statement.keywords),
            *statement.body,
        ]
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


def _has_original_source_failure(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    *,
    require_not_implemented: bool = False,
) -> bool:
    """Bind a location-only failure to the test source that existed when RED started."""
    node = _original_test_node(test, evidence, run)
    if node is None or not validation_run_covers_test(run.core_command, run.output, test):
        return False
    latest = max(
        (
            edit
            for edit in evidence.edits
            if edit.session_id == run.session_id
            and edit.path == test.path
            and edit.timestamp < run.started_at
        ),
        key=lambda edit: edit.order,
        default=None,
    )
    if latest is None or latest.source_after is None:
        # A partial Edit proves an API call, but cannot establish absolute line numbers.
        return False
    lines = (run.output or "").splitlines()
    start = 0
    for index, line in enumerate(lines):
        match = _PYTEST_LOCATION_RE.match(line)
        if match is None:
            continue
        section = "\n".join(lines[start : index + 1])
        start = index + 1
        if (
            _path_matches_artifact(match.group("path"), test)
            and node.lineno < int(match.group("line")) <= (node.end_lineno or node.lineno)
            and _section_has_failure_detail(section)
            and (not require_not_implemented or "NotImplementedError" in section)
        ):
            return True
    return False


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
            and _is_production_edit_path(edit.path)
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
            and _is_production_edit_path(edit.path)
            and not edit.source_unchanged
        ):
            latest_by_path[edit.path] = edit
    for edit in latest_by_path.values():
        if (
            not edit.source_created
            or not _source_confirmed_before(edit, run)
            or edit.source_after is None
        ):
            continue
        classes = python_noop_module(edit.source_after)
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


def _has_named_red_failure(
    command: str, output: str | None, test: AcceptanceTest
) -> tuple[bool, str]:
    if not output:
        return False, "run produced no output"
    matched, reason = _has_pytest_body_failure(command, output, test)
    if validation_run_names_test(command, output, test) and matched:
        return True, ""
    symbols = (
        test.symbol,
        test.symbol.replace(".", "::"),
        test.symbol.replace("::", "."),
    )
    symbol_patterns = tuple(
        re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])") for symbol in symbols
    )
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if _PYTEST_LOCATION_RE.match(line):
            continue
        if not any(pattern.search(line) for pattern in symbol_patterns):
            continue
        if _PASS_STATUS_RE.search(line):
            continue
        section = _failure_section(lines, index)
        if _ASSERTION_DETAIL_RE.search(section) and is_assertion_failure(section):
            return True, ""
    return False, reason


def _has_pytest_body_failure(
    command: str, output: str, test: AcceptanceTest, *, require_not_implemented: bool = False
) -> tuple[bool, str]:
    """Recognize a failure raised from a targeted pytest body, including RTK summaries."""
    artifact_nodes, same_file_nodes = _selected_pytest_nodes(command, test)
    lines = output.splitlines()
    has_attributable_section = False
    for index, line in enumerate(lines):
        header = _PYTEST_FAILURE_HEADER_RE.match(line)
        if header is None or not _header_names_artifact(
            header.group("name"), test, artifact_nodes, same_file_nodes
        ):
            continue
        section = _failure_section(lines, index)
        if _section_has_artifact_location(section, test):
            has_attributable_section = True
            if _section_has_failure_detail(section) and (
                not require_not_implemented or "NotImplementedError" in section
            ):
                return True, ""
    ordered_failure = _has_ordered_pytest_failure(
        lines, test, require_not_implemented=require_not_implemented
    )
    if ordered_failure:
        return True, ""
    if ordered_failure is False:
        return False, _red_section_rejection(test, has_attributable_section)
    if not artifact_nodes:
        # Location-only shapes (RTK, --tb=short) carry no failure header, so an
        # unqualified frame symbol is attributable only through explicit node
        # selection on the command line.
        return False, _red_section_rejection(test, has_attributable_section)
    for index, line in enumerate(lines):
        match = _PYTEST_LOCATION_RE.match(line)
        if match is None:
            continue
        reported_symbol = match.group("symbol")
        if not _path_matches_artifact(match.group("path"), test):
            continue
        if reported_symbol is None:
            # --tb=line names no symbol; the location is attributable only when
            # the artifact is the sole node the command selected in that file.
            if same_file_nodes != artifact_nodes or len(artifact_nodes) != 1:
                continue
        elif not _selected_node_matches(reported_symbol, artifact_nodes, same_file_nodes):
            continue
        has_attributable_section = True
        section = _failure_section(lines, index)
        if _section_has_failure_detail(section) and (
            not require_not_implemented or "NotImplementedError" in section
        ):
            return True, ""
    return False, _red_section_rejection(test, has_attributable_section)


def _has_ordered_pytest_failure(
    lines: list[str], test: AcceptanceTest, *, require_not_implemented: bool = False
) -> bool | None:
    """Pair --tb=line locations with ordered summaries; None means no complete report."""
    failures = next(
        (index for index, line in enumerate(lines) if line.strip("= ") == "FAILURES"), None
    )
    if failures is None:
        return None
    summary = next(
        (
            index
            for index in range(failures + 1, len(lines))
            if lines[index].strip("= ") == "short test summary info"
        ),
        None,
    )
    if summary is None:
        return None
    sections: list[str] = []
    start = failures + 1
    for index in range(start, summary):
        if _PYTEST_LOCATION_RE.match(lines[index]):
            sections.append("\n".join(lines[start : index + 1]))
            start = index + 1
    failed_nodes: list[str] = []
    for line in lines[summary + 1 :]:
        if line.startswith("FAILED "):
            parts = line.split(maxsplit=2)
            if len(parts) < 2:
                return False
            failed_nodes.append(parts[1])
    if len(sections) != len(failed_nodes):
        return False
    artifact = test.symbol.replace("::", ".")
    for node, section in zip(failed_nodes, sections, strict=True):
        path, separator, name = node.partition("::")
        if (
            separator
            and _path_matches_artifact(path, test)
            and name.replace("::", ".").split("[", maxsplit=1)[0] == artifact
            and _section_has_failure_detail(section)
            and (not require_not_implemented or "NotImplementedError" in section)
        ):
            return True
    return False


def _red_section_rejection(test: AcceptanceTest, has_attributable_section: bool) -> str:
    if has_attributable_section:
        return f"attributable failure section for {test.symbol!r} has no accepted failure detail"
    return f"no attributable failure section for {test.symbol!r}"


def _selected_pytest_nodes(
    command: str,
    test: AcceptanceTest,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return (), ()
    artifact_name = test.symbol.replace("::", ".")
    artifact_nodes: list[str] = []
    same_file_nodes: list[str] = []
    for token in tokens:
        node_path, separator, node_name = token.partition("::")
        if not separator or not _path_matches_artifact(node_path, test):
            continue
        normalized_name = node_name.replace("::", ".").split("[", maxsplit=1)[0]
        same_file_nodes.append(normalized_name)
        if normalized_name == artifact_name or normalized_name.startswith(f"{artifact_name}."):
            artifact_nodes.append(normalized_name)
    return tuple(dict.fromkeys(artifact_nodes)), tuple(dict.fromkeys(same_file_nodes))


def _selected_node_matches(
    reported: str,
    artifact_nodes: tuple[str, ...],
    same_file_nodes: tuple[str, ...],
) -> bool:
    reported = reported.split("[", maxsplit=1)[0]
    truncated = reported.endswith("...")
    if truncated:
        reported = reported.removesuffix("...")
    if "." in reported:
        for node in artifact_nodes:
            if (truncated and node.startswith(reported)) or node == reported:
                return True
            if node.rsplit(".", maxsplit=1)[-1].startswith("Test") and reported.startswith(
                f"{node}."
            ):
                return True
        return False
    matching_nodes = tuple(
        node for node in same_file_nodes if _node_leaf_matches(node, reported, truncated=truncated)
    )
    if len(matching_nodes) == 1:
        return matching_nodes[0] in artifact_nodes
    if len(same_file_nodes) == 1 and artifact_nodes[0].rsplit(".", maxsplit=1)[-1].startswith(
        "Test"
    ):
        return reported.startswith("test_")
    return False


def _header_names_artifact(
    reported: str,
    test: AcceptanceTest,
    artifact_nodes: tuple[str, ...],
    same_file_nodes: tuple[str, ...],
) -> bool:
    """Match a FAILURES section header to the acceptance artifact.

    pytest prints the qualified test name (``TestClass.test_method`` or
    ``test_function``) in the header, so a file, class, or ``-k`` selection on
    the command line still attributes exactly; explicit node selection keeps
    its stricter same-file disambiguation.
    """
    if artifact_nodes:
        return _selected_node_matches(reported, artifact_nodes, same_file_nodes)
    name = reported.split("[", maxsplit=1)[0]
    artifact = test.symbol.replace("::", ".")
    return name == artifact or name.startswith(f"{artifact}.")


def _node_leaf_matches(node: str, reported: str, *, truncated: bool) -> bool:
    leaf = node.rsplit(".", maxsplit=1)[-1]
    return leaf.startswith(reported) if truncated else leaf == reported


def _section_has_artifact_location(section: str, test: AcceptanceTest) -> bool:
    return any(
        match is not None and _path_matches_artifact(match.group("path"), test)
        for match in (_PYTEST_LOCATION_RE.match(line) for line in section.splitlines())
    )


def _section_has_failure_detail(section: str) -> bool:
    return bool(
        _ASSERTION_DETAIL_RE.search(section)
        or _PYTEST_FAIL_DETAIL_RE.search(section)
        or _PYTHON_EXCEPTION_DETAIL_RE.search(section)
        or _RAISE_EXCEPTION_DETAIL_RE.search(section)
    )


def _path_matches_artifact(path: str, test: AcceptanceTest) -> bool:
    expected_parts = PurePosixPath(test.path).parts
    reported_parts = PurePosixPath(path).parts
    return reported_parts[-len(expected_parts) :] == expected_parts


def _failure_section(lines: list[str], start: int) -> str:
    end = min(len(lines), start + 120)
    for boundary in range(start + 1, end):
        if _FAILURE_SECTION_BOUNDARY_RE.match(lines[boundary]):
            end = boundary
            break
    return "\n".join(lines[start:end])


def _is_test_execution_run(run: TranscriptValidationRun) -> bool:
    if run.matcher_id in _NON_EXECUTION_TEST_MATCHERS:
        return False
    if run.validation_segments:
        return any(
            "test" in segment.categories
            and not segment.command.startswith("gobby test-quality audit")
            for segment in run.validation_segments
        )
    return "test" in run.categories


def _find_green_run(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    red: TranscriptValidationRun,
    *,
    after_order: int,
) -> TranscriptValidationRun | None:
    for run in sorted(evidence.validation_runs, key=lambda item: item.order):
        if (
            run.outcome != "success"
            or not _is_test_execution_run(run)
            or run.order <= max(red.order, after_order)
        ):
            continue
        if validation_run_covers_test(run.core_command, run.output, test):
            return run
    return None


__all__ = ["TddEvidenceResult", "evaluate_tdd_evidence"]
