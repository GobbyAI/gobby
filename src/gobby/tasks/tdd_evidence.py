"""Deterministic red/green evidence checks for named acceptance tests."""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from gobby.tasks.acceptance_artifacts import (
    AcceptanceTest,
    is_assertion_failure,
    panic_has_message,
    validation_run_covers_test,
    validation_run_names_test,
)
from gobby.tasks.tdd_compacted_output import (
    _COMPACTED_RECALL_RE,
    _PYTEST_FAIL_DETAIL_RE,
    _PYTEST_FAILURE_HEADER_RE,
    _PYTHON_EXCEPTION_DETAIL_RE,
    _RTK_FAILURE_HEADER_RE,
    _path_matches_artifact,
    has_compacted_pytest_failure,
)
from gobby.tasks.tdd_paths import is_implementation_edit_path, is_test_convention_path
from gobby.tasks.tdd_pytest_provenance import runtime_pytest_output, terminal_runtime_frame
from gobby.tasks.tdd_python_evidence import (
    PythonBindingCache,
    _has_pytest_fail_placeholder,
    _has_python_keyword_stub,
    _has_python_module_stub,
    _has_python_runtime_stub,
    _has_python_test_tightening,
    _has_python_unchanged_api,
    _original_test_module,
    _original_test_node,
    _retains_python_red_source,
)
from gobby.tasks.tdd_retained_output import delivered_proof_runs
from gobby.tasks.tdd_stub_revert import has_stub_revert_proof
from gobby.tasks.transcript_background import unread_reason
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_tool_arguments import PythonModuleCache

_ASSERTION_DETAIL_RE = re.compile(
    r"AssertionError|assertion failed|\bassert\b|panicked at|Failed:\s+DID NOT RAISE",
    re.IGNORECASE,
)
_PYTEST_LOCATION_RE = re.compile(
    r"^\s*(?P<path>\S+\.py):(?P<line>\d+):"
    r"(?: in (?P<symbol>\S+)| (?:[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)|Failed)(?::.*)?"
    r"| assert(?:\s+.*)?)?\s*$"
)
_RAISE_EXCEPTION_DETAIL_RE = re.compile(
    r"^[ \t]*>?[ \t]*raise[ \t]+[A-Za-z_][\w.]*(?:Error|Exception)\b",
    re.MULTILINE,
)
_PASS_STATUS_RE = re.compile(r"\b(?:PASSED|SKIPPED|XFAIL|XPASS)\b", re.IGNORECASE)
_FAILURE_SECTION_BOUNDARY_RE = re.compile(
    r"^(?:_+\s+(?![_\s]+$)\S.*\s+_+|\s*\d+\.\s+\[FAIL\]\s+\S.*|"
    r"(?:FAILED|ERROR|PASSED|SKIPPED)\s+\S.*|"
    r"\s*(?:FAIL|PASS)\s+\S+::\S+\s+\S+\s*$|"
    r".*::\S+\s+(?:PASSED|FAILED|ERROR|SKIPPED)\b|"
    # Each Rust node's block ends where the next nextest status line or libtest
    # per-test header starts, so a sibling's panic never lands in this section.
    r"\s*(?:[A-Z][A-Z-]*|TRY \d+ [A-Z-]+)\s+\[\s*[\d.]+s\]|"
    r"-{3,4} (?:\S+ std(?:out|err) -{4}|STD(?:OUT|ERR):\s))",
    re.IGNORECASE,
)
_NON_EXECUTION_TEST_MATCHERS = frozenset({"gobby-test-quality-audit"})
# (path, symbol, session, run order, require_not_implemented, window edit order)
_RedCacheKey = tuple[str, str, str, int, bool, int]


_NOT_IMPLEMENTED_DETAIL_RE = re.compile(
    r"(?m)^(?:\s*E\s+|[^\n]+\.py:\d+:\s*)NotImplementedError(?::|\s*$)"
)


@dataclass(frozen=True, slots=True)
class TddEvidenceResult:
    """TDD evidence outcome for a close attempt."""

    passed: bool
    skipped: bool
    findings: tuple[str, ...]
    red_runs: tuple[str, ...] = ()
    green_runs: tuple[str, ...] = ()
    regression_tests: tuple[str, ...] = ()

    def details(self) -> dict[str, object]:
        return {
            "findings": list(self.findings),
            "red_runs": list(self.red_runs),
            "green_runs": list(self.green_runs),
            "regression_tests": list(self.regression_tests),
        }


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


def _contains_word(value: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", value) is not None


def evaluate_tdd_evidence(
    tests: tuple[AcceptanceTest, ...],
    evidence: TranscriptEvidence,
    *,
    renamed_test_paths: Mapping[str, tuple[str, ...]] | None = None,
    task_category: str | None = None,
    implementation_paths: Iterable[str] | None = None,
) -> TddEvidenceResult:
    """Require a cycle for changed tests and later coverage of every named test.

    The caller supplies implementation_paths from the task's linked patch. Only
    test-category tasks may count non-test fixture modules under tests/ as code.
    Unchanged named tests contribute either a genuine cycle or regression runs.
    Unchanged detection is file-granular: every named test in an edited file is
    treated as changed. Regression listings accompany a genuine task-level cycle.
    """
    if not tests:
        return TddEvidenceResult(
            False, False, ("TDD is required but no named test reference resolved.",)
        )

    named_test_paths = frozenset(
        path
        for test in tests
        for path in (test.path, *(renamed_test_paths or {}).get(test.path, ()))
    )
    linked_paths = frozenset(implementation_paths or ())
    infrastructure_paths = linked_paths if task_category == "test" else frozenset()

    parse_cache: PythonModuleCache = {}
    binding_cache: PythonBindingCache = {}
    red_cache: dict[_RedCacheKey, tuple[bool, str | None]] = {}
    findings: list[str] = []
    cycle: tuple[TranscriptValidationRun, TranscriptEdit] | None = None
    # Attribution limits affirmative anchors; safety checks need physical history.
    task_edits = tuple(edit for edit in evidence.edits if edit.task_attributed)
    changed_paths = linked_paths | {edit.path for edit in task_edits}
    changed_test_paths = {
        path
        for path in changed_paths
        if (path in named_test_paths or is_test_convention_path(path))
        and not is_implementation_edit_path(
            path,
            named_test_paths=named_test_paths,
            test_infrastructure_paths=infrastructure_paths,
        )
    }
    unchanged = tuple(
        test
        for test in tests
        if implementation_paths is not None
        and not changed_paths.intersection(
            (test.path, *(renamed_test_paths or {}).get(test.path, ()))
        )
    )
    first_production = min(
        (
            edit
            for edit in task_edits
            if is_implementation_edit_path(
                edit.path,
                named_test_paths=named_test_paths,
                test_infrastructure_paths=infrastructure_paths,
            )
        ),
        key=lambda edit: edit.order,
        default=None,
    )
    unchanged_reds: dict[str, TranscriptValidationRun] = {}
    if unchanged and first_production is None:
        return TddEvidenceResult(
            False, False, ("Unchanged-test evidence has no attributable production edit.",)
        )
    if first_production is not None:
        for test in unchanged:
            red, _ = _find_red_run(test, evidence, -1, first_production)
            if (
                red is not None
                and _find_green_run(test, evidence, red, after_order=first_production.order)
                is not None
            ):
                unchanged_reds[test.reference] = red
    for test in tests:
        if test in unchanged:
            continue
        test_paths = (test.path, *(renamed_test_paths or {}).get(test.path, ()))
        test_edits = sorted(
            (edit for edit in task_edits if edit.path in test_paths),
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
                    for edit in task_edits
                    if edit.order > test_edit.order
                    and is_implementation_edit_path(
                        edit.path,
                        named_test_paths=named_test_paths,
                        test_infrastructure_paths=infrastructure_paths,
                    )
                ),
                key=lambda edit: edit.order,
            )
            if not production_edits:
                continue
            production_edit_seen = True
            production_edit = production_edits[0]
            window_red, window_rejection = _find_red_run(
                red_test,
                evidence,
                test_edit.order,
                production_edit,
                require_not_implemented=not test.path.endswith(".rs")
                and any(
                    edit.order < test_edit.order
                    and is_implementation_edit_path(
                        edit.path,
                        named_test_paths=named_test_paths,
                        test_infrastructure_paths=infrastructure_paths,
                    )
                    for edit in evidence.edits
                ),
                parse_cache=parse_cache,
                binding_cache=binding_cache,
                red_cache=red_cache,
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
                        parse_cache=parse_cache,
                        binding_cache=binding_cache,
                        red_cache=red_cache,
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

    if cycle is None and (changed_test_paths or not unchanged):
        if not findings:
            findings.append("TDD requires a named RED-to-GREEN cycle for the task's changed tests")
        return TddEvidenceResult(False, False, tuple(findings))

    # An existing test may expose a bug without changing. It can anchor a cycle
    # only when this task did not add or change tests that still need their RED.
    if cycle is None and unchanged_reds and first_production is not None:
        cycle = (min(unchanged_reds.values(), key=lambda run: run.order), first_production)
    red = cycle[0] if cycle is not None else None
    after_order = (
        cycle[1].order
        if cycle is not None
        else max((edit.order for edit in task_edits), default=-1)
    )
    findings = []
    if cycle is None:
        findings.append("TDD requires at least one genuine RED-to-GREEN cycle for the task")
    green_commands: list[str] = []
    for test in tests:
        green = _find_green_run(
            test,
            evidence,
            red,
            after_order=after_order,
        )
        if green is None:
            requirement = (
                "the production edit that completed the task-level TDD cycle"
                if cycle is not None
                else "the task's latest edit (regression evidence)"
            )
            findings.append(f"{test.reference}: no pass after {requirement}")
            continue
        green_commands.append(green.command)

    return TddEvidenceResult(
        passed=not findings,
        skipped=False,
        findings=tuple(findings),
        red_runs=tuple(
            dict.fromkeys(
                (
                    *((red.command,) if red is not None else ()),
                    *(run.command for run in unchanged_reds.values()),
                )
            )
        ),
        green_runs=tuple(green_commands),
        regression_tests=tuple(
            test.reference for test in unchanged if test.reference not in unchanged_reds
        ),
    )


def _find_red_run(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    test_edit_order: int,
    first_non_test_edit: TranscriptEdit | None,
    *,
    require_not_implemented: bool = False,
    parse_cache: PythonModuleCache | None = None,
    binding_cache: PythonBindingCache | None = None,
    red_cache: dict[_RedCacheKey, tuple[bool, str | None]] | None = None,
) -> tuple[TranscriptValidationRun | None, str | None]:
    """Find the first RED before ``first_non_test_edit``.

    An original job's retained output may supply its failure bodies only when it
    settled before ``first_non_test_edit``, the edit that closes this window.
    """
    rejection = None
    production_at = first_non_test_edit.timestamp if first_non_test_edit is not None else None
    window = first_non_test_edit.order if first_non_test_edit is not None else -1
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
        key = (
            test.path,
            test.symbol,
            run.session_id,
            run.order,
            require_not_implemented,
            window,
        )
        cached = red_cache.get(key) if red_cache is not None else None
        if cached is None:
            # A background run's joined display is never a candidate: each recovered
            # delivery is proved alone, then the authenticated retained output.
            candidates = [] if run.background_output_path is not None else [run]
            cached = (False, None)
            for proof_run in candidates + delivered_proof_runs(run, production_at):
                matched, proof_reason = _red_run_proof(
                    test,
                    evidence,
                    proof_run,
                    core_command,
                    require_not_implemented,
                    parse_cache,
                    binding_cache,
                    first_non_test_edit,
                )
                if matched:
                    cached = (True, None)
                    break
                cached = (False, cached[1] or proof_reason)
            if red_cache is not None:
                red_cache[key] = cached
        matched, reason = cached
        if matched:
            return run, None
        if not reason and run.background_output_path is not None and rejection is None:
            reason = unread_reason(run, production_at, _rust_node(test))
        if reason is None:
            continue
        rejection = f"run {run.command!r} rejected: {reason}"
    return None, rejection


def _rust_node(test: AcceptanceTest) -> str:
    """The full module path nextest names a Rust test by, else its symbol."""
    return "::".join((*(test.rust_module or ()), test.symbol))


def _red_run_proof(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    core_command: str,
    require_not_implemented: bool,
    parse_cache: PythonModuleCache | None,
    binding_cache: PythonBindingCache | None,
    closing_edit: TranscriptEdit | None,
) -> tuple[bool, str | None]:
    source_failure = _has_original_source_failure(test, evidence, run, parse_cache=parse_cache)
    if not source_failure and not validation_run_names_test(core_command, run.output, test):
        return False, None
    matched, reason = _has_named_red_failure(
        core_command, run.output, test, _original_test_module(test, evidence, run, parse_cache)
    )
    matched = matched or source_failure
    if matched:
        if _has_pytest_fail_placeholder(test, evidence, run, parse_cache):
            reason = "test body is an unconditional pytest.fail placeholder"
        elif not require_not_implemented or (
            _has_runtime_stub(test, evidence, run, core_command, parse_cache)
            or _has_python_keyword_stub(test, evidence, run, parse_cache)
            or _has_python_module_stub(test, evidence, run, parse_cache)
            or _has_python_unchanged_api(test, evidence, run, parse_cache, binding_cache)
            or _test_tightening_proof(test, evidence, run, parse_cache)
            or has_stub_revert_proof(evidence, run, closing_edit)
        ):
            return True, None
        else:
            reason = (
                "post-production red has no attributable NotImplementedError or proven API stub"
            )
    return False, reason


def _has_runtime_stub(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    command: str,
    parse_cache: PythonModuleCache | None,
) -> bool:
    """Bind an HTTP/helper-mediated stub failure to its named test section."""
    nodes, same_file = _selected_pytest_nodes(command, test)
    # Captured payload can forge even a fresh failure header. Never resume inside it.
    output = re.split(r"(?m)^-+\s+Captured (?:stdout|stderr|log)\b", run.output or "", maxsplit=1)[
        0
    ]
    # Verify source spans before a local repr can forge fresh headers or frame markers.
    source_output = runtime_pytest_output(output, evidence, run, parse_cache)
    if source_output is None:
        return False
    lines = output.splitlines()
    for index, line in enumerate(lines):
        header = _PYTEST_FAILURE_HEADER_RE.match(line) or _RTK_FAILURE_HEADER_RE.match(line)
        if header is None or not _header_names_artifact(
            header.group("name"), test, nodes, same_file
        ):
            continue
        original_section = _failure_section(lines, index)
        section = runtime_pytest_output(original_section, evidence, run, parse_cache)
        if section is None or section not in source_output:
            continue
        terminal = terminal_runtime_frame(section, original_section)
        if (
            _section_has_artifact_location(section, test)
            and terminal is not None
            and _has_python_runtime_stub(terminal, evidence, run, parse_cache, command=command)
        ):
            return True
    return False


def _test_tightening_proof(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    red: TranscriptValidationRun,
    parse_cache: PythonModuleCache | None = None,
) -> bool:
    """Verify transcript and source continuity, not semantic non-interference.

    Accepted residuals include pre-existing executable mutation in an existing
    test node, an unchanged module helper, or a pre-GREEN fixture.
    """
    prior_green = max(
        (
            candidate
            for candidate in evidence.validation_runs
            if candidate.session_id == red.session_id
            and candidate.outcome == "success"
            and candidate.order < red.order
            and candidate.completed_at is not None
            and candidate.completed_at < red.started_at
            and _is_test_execution_run(candidate)
            and _green_run_covers_test(candidate, test)
        ),
        key=lambda candidate: candidate.order,
        default=None,
    )
    return _has_python_test_tightening(test, evidence, red, prior_green, parse_cache)


def _has_original_source_failure(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    *,
    require_not_implemented: bool = False,
    parse_cache: PythonModuleCache | None = None,
) -> bool:
    """Bind a location-only failure to the test source that existed when RED started."""
    node = _original_test_node(test, evidence, run, parse_cache)
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
            and (not require_not_implemented or _NOT_IMPLEMENTED_DETAIL_RE.search(section))
        ):
            return True
    return False


def _has_named_red_failure(
    command: str, output: str | None, test: AcceptanceTest, module: ast.Module | None
) -> tuple[bool, str]:
    if not output:
        return False, "run produced no output"
    if _has_failed_node_summary(output, test) or has_compacted_pytest_failure(output, test, module):
        return True, ""
    matched, reason = _has_pytest_body_failure(command, output, test)
    if validation_run_names_test(command, output, test) and matched:
        return True, ""
    if test.path.endswith(".py"):
        return False, reason
    symbols = (
        test.symbol,
        test.symbol.replace(".", "::"),
        test.symbol.replace("::", "."),
        _rust_node(test),
    )
    lines = output.splitlines()
    section_node: str | None = None
    for index, line in enumerate(lines):
        # Node identity comes from runner headers, never assertion text that
        # happens to mention a target symbol inside a sibling's failure body.
        header = re.fullmatch(r"\s*---- (\S+) std(?:out|err) ----\s*", line) or re.fullmatch(
            r"\s*FAIL \S+::\S+ (\S+)\s*", line
        )
        if _FAILURE_SECTION_BOUNDARY_RE.match(line):
            section_node = header[1] if header else None
        panic = re.match(r"\s*thread '([^']+)'(?: \(\d+\))? panicked at\b", line)
        node = header[1] if header else panic[1] if panic else None
        if node not in symbols or (section_node is not None and section_node not in symbols):
            continue
        section = _failure_section(lines, index)
        panic_nodes = re.findall(r"(?m)^\s*thread '([^']+)'(?: \(\d+\))? panicked at\b", section)
        if any(name not in symbols for name in panic_nodes):
            continue
        if (
            _ASSERTION_DETAIL_RE.search(section)
            and is_assertion_failure(section)
            and panic_has_message(section)
        ):
            return True, ""
    return False, reason


def _has_failed_node_summary(output: str, test: AcceptanceTest) -> bool:
    """Credit pytest summaries or legacy sections carrying node-bound assertion detail."""
    if not test.path.endswith(".py"):
        return False
    expected = test.symbol.replace("::", ".")
    lines = output.splitlines()
    for index, line in enumerate(lines):
        match = re.fullmatch(r"\s*FAILED\s+(\S+\.py)::(\S+)(?:\s+-\s+(.+))?", line) or re.fullmatch(
            r"\s*(\S+\.py)::(\S+)\s+FAILED(?:\s+-\s+(.+))?", line
        )
        if match is None or not _path_matches_artifact(match[1], test):
            continue
        reported = match[2].replace("::", ".")
        if "[" not in expected:
            reported = reported.split("[", maxsplit=1)[0]
        if reported != expected:
            continue
        detail = match[3]
        if detail is None:
            # Older provider output uses FAILED as a section header before E assert.
            # A complete pytest report instead belongs to the ordered body parser.
            if _has_ordered_pytest_failure(lines, test) is not None:
                continue
            section = _failure_section(lines, index)
            if (
                index + 1 < len(lines)
                and re.match(r"\s*E\s+(?:assert\b|AssertionError\b)", lines[index + 1])
                and not any(_PYTEST_LOCATION_RE.match(item) for item in section.splitlines())
                and is_assertion_failure(section)
            ):
                return True
            continue
        # Identity alone cannot recover discarded failure detail. In particular,
        # an assertion elsewhere in this run belongs to its own node only.
        if re.match(
            r"(?:AssertionError\b|assert\s|Failed:\s+DID NOT RAISE\b)", detail
        ) and is_assertion_failure(detail):
            return True
    return False


def _has_pytest_body_failure(
    command: str, output: str, test: AcceptanceTest, *, require_not_implemented: bool = False
) -> tuple[bool, str]:
    """Recognize a failure raised from a targeted pytest body, including RTK summaries."""
    artifact_nodes, same_file_nodes = _selected_pytest_nodes(command, test)
    lines = output.splitlines()
    has_attributable_section = False
    for index, line in enumerate(lines):
        header = _PYTEST_FAILURE_HEADER_RE.match(line) or _RTK_FAILURE_HEADER_RE.match(line)
        if header is None or not _header_names_artifact(
            header.group("name"), test, artifact_nodes, same_file_nodes
        ):
            continue
        section = _failure_section(lines, index)
        if _section_has_artifact_location(section, test) or (
            artifact_nodes
            and not any(_PYTEST_LOCATION_RE.match(row) for row in section.splitlines())
            and _ASSERTION_DETAIL_RE.search(section)
            and is_assertion_failure(section)
        ):
            has_attributable_section = True
            if _section_has_failure_detail(section) and (
                not require_not_implemented or _NOT_IMPLEMENTED_DETAIL_RE.search(section)
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
            # Compacted output has no location proof beyond that selection, so
            # its location must bind through _has_original_source_failure instead.
            if (
                _COMPACTED_RECALL_RE.search(output)
                or same_file_nodes != artifact_nodes
                or len(artifact_nodes) != 1
            ):
                continue
        elif not _selected_node_matches(reported_symbol, artifact_nodes, same_file_nodes):
            continue
        has_attributable_section = True
        section = _failure_section(lines, index)
        if _section_has_failure_detail(section) and (
            not require_not_implemented or _NOT_IMPLEMENTED_DETAIL_RE.search(section)
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
            and (not require_not_implemented or _NOT_IMPLEMENTED_DETAIL_RE.search(section))
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
    red: TranscriptValidationRun | None,
    *,
    after_order: int,
) -> TranscriptValidationRun | None:
    retain_source = red is not None and _test_tightening_proof(test, evidence, red)
    for run in sorted(evidence.validation_runs, key=lambda item: item.order):
        if (
            run.outcome != "success"
            or not _is_test_execution_run(run)
            or run.order <= max(red.order if red is not None else -1, after_order)
        ):
            continue
        if _green_run_covers_test(run, test) and (
            not retain_source
            or (red is not None and _retains_python_red_source(test, evidence, red, run))
        ):
            return run
    return None


def _green_run_covers_test(run: TranscriptValidationRun, test: AcceptanceTest) -> bool:
    if not validation_run_covers_test(run.core_command, run.output, test):
        return False
    if not test.path.endswith(".py"):
        return True
    try:
        tokens = shlex.split(run.core_command or "")
    except ValueError:
        return False
    pytest_index = next(
        (
            index
            for index, token in enumerate(tokens)
            if PurePosixPath(token).name in {"pytest", "py.test"}
        ),
        None,
    )
    if pytest_index is not None:
        tokens = tokens[pytest_index + 1 :]
    for index, token in enumerate(tokens):
        if token.startswith("-m"):
            return validation_run_names_test("", run.output, test)
        if not token.startswith("-k"):
            continue
        expression = (
            tokens[index + 1]
            if token == "-k" and index + 1 < len(tokens)
            else token[2:].removeprefix("=")
        )
        # A simple pytest keyword matches the node's path/class/function names.
        # Complex expressions need an explicit node receipt in the output.
        if not re.fullmatch(r"\w+", expression) or expression.lower() not in test.reference.lower():
            return validation_run_names_test("", run.output, test)
    artifact_nodes, same_file_nodes = _selected_pytest_nodes(run.core_command or "", test)
    if not same_file_nodes or artifact_nodes:
        return True
    if any(_path_matches_artifact(token, test) for token in tokens if "::" not in token):
        return True
    # Selecting a class covers its methods; selecting a sibling method does not.
    name = test.symbol.replace("::", ".")
    return any(name.startswith(f"{selected}.") for selected in same_file_nodes)


__all__ = ["TddEvidenceResult", "evaluate_tdd_evidence"]
