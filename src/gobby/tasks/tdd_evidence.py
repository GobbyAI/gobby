"""Deterministic red/green evidence checks for named acceptance tests."""

from __future__ import annotations

from dataclasses import dataclass

from gobby.tasks.acceptance_artifacts import (
    AcceptanceTest,
    is_assertion_failure,
    validation_run_covers_test,
    validation_run_names_test,
)
from gobby.tasks.epic_guards import is_test_convention_path
from gobby.tasks.transcript_evidence import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
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


def evaluate_tdd_evidence(
    tests: tuple[AcceptanceTest, ...],
    evidence: TranscriptEvidence,
) -> TddEvidenceResult:
    """Require assertion-backed red before implementation and a later pass."""
    if not tests:
        return TddEvidenceResult(True, True, ())

    findings: list[str] = []
    red_commands: list[str] = []
    green_commands: list[str] = []
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
        for test_edit in test_edits:
            first_non_test_edit = min(
                (
                    edit
                    for edit in evidence.edits
                    if not is_test_convention_path(edit.path) and edit.order > test_edit.order
                ),
                key=lambda edit: edit.order,
                default=None,
            )
            if first_non_test_edit is None:
                continue
            red = _find_red_run(test, evidence, test_edit.order, first_non_test_edit)
            if red is None:
                continue
            green = _find_green_run(
                test,
                evidence,
                red,
                after_order=first_non_test_edit.order,
            )
            if green is not None:
                break
        if red is None:
            findings.append(
                f"{test.reference}: missing assertion or panic failure after the test edit "
                "and before the first non-test edit"
            )
            continue
        red_commands.append(red.command)
        if green is None:
            findings.append(
                f"{test.reference}: assertion-backed red has no later production edit and pass"
            )
            continue
        green_commands.append(green.command)

    return TddEvidenceResult(
        passed=not findings,
        skipped=False,
        findings=tuple(findings),
        red_runs=tuple(red_commands),
        green_runs=tuple(green_commands),
    )


def _find_red_run(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    test_edit_order: int,
    first_non_test_edit: TranscriptEdit | None,
) -> TranscriptValidationRun | None:
    for run in sorted(evidence.validation_runs, key=lambda item: item.order):
        if run.outcome != "failure" or run.order <= test_edit_order:
            continue
        if first_non_test_edit is not None and run.order >= first_non_test_edit.order:
            continue
        if not validation_run_names_test(run.command, run.output, test):
            continue
        if is_assertion_failure(run.output):
            return run
    return None


def _find_green_run(
    test: AcceptanceTest,
    evidence: TranscriptEvidence,
    red: TranscriptValidationRun,
    *,
    after_order: int,
) -> TranscriptValidationRun | None:
    for run in sorted(evidence.validation_runs, key=lambda item: item.order):
        if run.outcome != "success" or run.order <= max(red.order, after_order):
            continue
        if validation_run_covers_test(run.command, run.output, test):
            return run
    return None


__all__ = ["TddEvidenceResult", "evaluate_tdd_evidence"]
