"""Deterministic red/green evidence checks for named acceptance tests."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from gobby.tasks.acceptance_artifacts import (
    AcceptanceTest,
    is_assertion_failure,
    validation_run_covers_test,
    validation_run_names_test,
)
from gobby.tasks.transcript_evidence import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)

_ASSERTION_DETAIL_RE = re.compile(
    r"AssertionError|assertion failed|\bassert\b|panicked at",
    re.IGNORECASE,
)
_PYTEST_FAILURE_HEADER_RE = re.compile(r"^_{2,}\s+(?P<name>\S+)\s+_{2,}\s*$")
_PYTEST_LOCATION_RE = re.compile(
    r"^\s*(?P<path>\S+\.py):\d+:"
    r"(?: in (?P<symbol>\S+)| [A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))?\s*$"
)
_PYTHON_EXCEPTION_DETAIL_RE = re.compile(
    r"^\s*E\s+(?:[A-Za-z_][A-Za-z0-9_.]*)(?:Error|Exception):",
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
) -> TddEvidenceResult:
    """Require one assertion-backed cycle and later coverage of every named test."""
    if not tests:
        return TddEvidenceResult(True, True, ())

    findings: list[str] = []
    cycle: tuple[TranscriptValidationRun, TranscriptEdit] | None = None
    for test in tests:
        test_edits = sorted(
            (edit for edit in evidence.edits if edit.path == test.path),
            key=lambda edit: edit.order,
        )
        if not test_edits:
            findings.append(f"{test.reference}: transcript has no edit of the named test")
            continue
        red = None
        green = None
        production_edit_seen = False
        for test_edit in test_edits:
            first_production_edit = min(
                (
                    edit
                    for edit in evidence.edits
                    if edit.order > test_edit.order and _is_production_edit_path(edit.path)
                ),
                key=lambda edit: edit.order,
                default=None,
            )
            if first_production_edit is None:
                continue
            production_edit_seen = True
            window_red = _find_red_run(test, evidence, test_edit.order, first_production_edit)
            if window_red is None:
                continue
            if red is None:
                red = window_red
            green = _find_green_run(
                test,
                evidence,
                window_red,
                after_order=first_production_edit.order,
            )
            if green is not None:
                red = window_red
                cycle = (red, first_production_edit)
                break
        if cycle is not None:
            break
        if not production_edit_seen:
            findings.append(f"{test.reference}: no production edit follows the test edit")
            continue
        if red is None:
            findings.append(
                f"{test.reference}: missing assertion or panic failure after the test edit "
                "and before the first production edit"
            )
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
) -> TranscriptValidationRun | None:
    for run in sorted(evidence.validation_runs, key=lambda item: item.order):
        if (
            run.outcome != "failure"
            or not _is_test_execution_run(run)
            or run.order <= test_edit_order
        ):
            continue
        if first_non_test_edit is not None and run.order >= first_non_test_edit.order:
            continue
        if not validation_run_names_test(run.command, run.output, test):
            continue
        if _has_named_red_failure(run.command, run.output, test):
            return run
    return None


def _has_named_red_failure(command: str, output: str | None, test: AcceptanceTest) -> bool:
    if not output:
        return False
    if validation_run_names_test(command, None, test) and _has_pytest_body_failure(
        command, output, test
    ):
        return True
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
        if not any(pattern.search(line) for pattern in symbol_patterns):
            continue
        if _PASS_STATUS_RE.search(line):
            continue
        section = _failure_section(lines, index)
        if _ASSERTION_DETAIL_RE.search(section) and is_assertion_failure(section):
            return True
    return False


def _has_pytest_body_failure(command: str, output: str, test: AcceptanceTest) -> bool:
    """Recognize a failure raised from a targeted pytest body, including RTK summaries."""
    if not is_assertion_failure(output):
        return False
    artifact_nodes, same_file_nodes = _selected_pytest_nodes(command, test)
    if not artifact_nodes:
        return False
    lines = output.splitlines()
    for index, line in enumerate(lines):
        header = _PYTEST_FAILURE_HEADER_RE.match(line)
        if header is None or not _selected_node_matches(
            header.group("name"), artifact_nodes, same_file_nodes
        ):
            continue
        section = _failure_section(lines, index)
        if _section_has_artifact_location(section, test) and _section_has_failure_detail(section):
            return True
    for index, line in enumerate(lines):
        match = _PYTEST_LOCATION_RE.match(line)
        if match is None:
            continue
        reported_symbol = match.group("symbol")
        if (
            reported_symbol is None
            or not _path_matches_artifact(match.group("path"), test)
            or not _selected_node_matches(reported_symbol, artifact_nodes, same_file_nodes)
        ):
            continue
        section = _failure_section(lines, index)
        if _section_has_failure_detail(section):
            return True
    return False


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


def _node_leaf_matches(node: str, reported: str, *, truncated: bool) -> bool:
    leaf = node.rsplit(".", maxsplit=1)[-1]
    return leaf.startswith(reported) if truncated else leaf == reported


def _section_has_artifact_location(section: str, test: AcceptanceTest) -> bool:
    return any(
        match is not None and _path_matches_artifact(match.group("path"), test)
        for match in (_PYTEST_LOCATION_RE.match(line) for line in section.splitlines())
    )


def _section_has_failure_detail(section: str) -> bool:
    return bool(_ASSERTION_DETAIL_RE.search(section) or _PYTHON_EXCEPTION_DETAIL_RE.search(section))


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
        if validation_run_covers_test(run.command, run.output, test):
            return run
    return None


__all__ = ["TddEvidenceResult", "evaluate_tdd_evidence"]
