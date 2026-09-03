"""Deterministic checklist facts used by the task-close lifecycle."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from gobby.config.shell_lexing import parse_shell_command
from gobby.tasks.transcript_evidence import TranscriptEvidence, TranscriptValidationRun
from gobby.tasks.transcript_outcomes import EvidenceOutcome, infer_failure_categories

GateStatus = Literal["passed", "failed", "skipped"]

_TEST_REQUIRED_CATEGORIES = frozenset({"code", "refactor", "test"})
_AUTO_PASS_CATEGORIES = frozenset({"docs", "planning", "research", "manual"})


@dataclass(frozen=True)
class CloseGateResult:
    """Result of one ordered close-checklist item."""

    item: int
    name: str
    status: GateStatus
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status != "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "name": self.name,
            "status": self.status,
            "passed": self.passed,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class CloseChecklist:
    """Ordered deterministic gate results, stopping at the first failure."""

    gates: tuple[CloseGateResult, ...]

    @property
    def ready(self) -> bool:
        return all(gate.passed for gate in self.gates)

    @property
    def first_failure(self) -> CloseGateResult | None:
        return next((gate for gate in self.gates if not gate.passed), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "gates": [gate.to_dict() for gate in self.gates],
        }


def first_failed_gate(gates: Iterable[CloseGateResult]) -> CloseChecklist:
    """Keep successful gates through the first failure and discard later work."""
    evaluated: list[CloseGateResult] = []
    for gate in gates:
        evaluated.append(gate)
        if not gate.passed:
            break
    return CloseChecklist(tuple(evaluated))


def evaluate_validation_commands(
    *,
    task_category: str | None,
    evidence: TranscriptEvidence,
    has_attributed_edits: bool,
) -> CloseGateResult:
    """Evaluate checklist item 9 from transcript-derived validation commands.

    Unknown outcomes are diagnostic only. A task-attributed edit makes every
    earlier run stale. Among fresh runs, the latest definitive outcome for each
    validation category wins, so a later clean run cures an earlier failure in
    the same category.
    """
    category = (task_category or "").strip().casefold()
    details = _validation_details(evidence)

    if not has_attributed_edits:
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="skipped",
            message="Validation command requirement skipped because the task has no attributed edits.",
            details={**details, "skip_reason": "no-edit"},
        )

    if category in _AUTO_PASS_CATEGORIES:
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="skipped",
            message=f"Validation command requirement skipped for task category '{category}'.",
            details={**details, "skip_reason": "category"},
        )

    fresh_runs = _fresh_runs(evidence)
    definitive = [run for run in fresh_runs if run.outcome != "unknown"]
    attributed, ambiguous = _attribute_compound_failures(definitive)
    latest_by_category = _latest_definitive_by_category(attributed)
    unresolved = {
        run_category: run
        for run_category, run in latest_by_category.items()
        if run.outcome == "failure"
    }
    unresolved_failures = [
        {
            "category": run_category,
            "command": run.command,
            "completed_at": run.completed_at.isoformat(),
        }
        for run_category, run in sorted(unresolved.items())
    ]
    ambiguous_failures = [
        {"command": run.command, "completed_at": run.completed_at.isoformat()} for run in ambiguous
    ]
    details = {
        **details,
        "fresh_run_count": len(fresh_runs),
        "latest_outcomes": {
            run_category: run.outcome for run_category, run in sorted(latest_by_category.items())
        },
        "unresolved_failure_categories": sorted(unresolved),
        "unresolved_failures": unresolved_failures,
        "ambiguous_compound_failures": ambiguous_failures,
    }

    if unresolved or ambiguous:
        blockers = [
            f"{failure['category']}: {failure['command']!r} at {failure['completed_at']}"
            for failure in unresolved_failures
        ]
        blockers.extend(
            f"unattributed compound command {failure['command']!r} at {failure['completed_at']}"
            for failure in ambiguous_failures
        )
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="failed",
            message=(
                f"A validation command is still failing ({'; '.join(blockers)}). "
                "Re-run each category clean after the final task edit; run ambiguous compound "
                "segments separately."
            ),
            details=details,
        )

    required_category = "test" if category in _TEST_REQUIRED_CATEGORIES else None
    if category == "config":
        has_success = any(run.outcome == "success" for run in latest_by_category.values())
    else:
        has_success = (
            required_category is not None
            and latest_by_category.get(required_category) is not None
            and latest_by_category[required_category].outcome == "success"
        )

    if has_success:
        message = "A clean validation command ran after the final task edit."
        if required_category:
            message = "A clean test-category validation command ran after the final task edit."
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="passed",
            message=message,
            details=details,
        )

    if required_category:
        cure = "Run a test-category validation command clean after the final task edit."
    elif category == "config":
        cure = "Run any recognized validation command clean after the final task edit."
    else:
        cure = (
            f"Task category '{category or 'unset'}' requires a recognized validation policy; "
            "set a supported category or run a clean validation command."
        )

    degraded = _degraded_message(evidence)
    message = f"{cure} {degraded}".strip()
    return CloseGateResult(
        item=9,
        name="validation_commands",
        status="failed",
        message=message,
        details=details,
    )


def _fresh_runs(evidence: TranscriptEvidence) -> list[TranscriptValidationRun]:
    if not evidence.edits:
        return list(evidence.validation_runs)
    last_edit_order = max(edit.order for edit in evidence.edits)
    return [run for run in evidence.validation_runs if run.order > last_edit_order]


def _latest_definitive_by_category(
    runs: Iterable[TranscriptValidationRun],
) -> dict[str, TranscriptValidationRun]:
    latest: dict[str, TranscriptValidationRun] = {}
    for run in sorted(runs, key=lambda item: (item.order, item.completed_at)):
        for category in run.categories:
            latest[category] = run
    return latest


def _attribute_compound_failures(
    runs: Iterable[TranscriptValidationRun],
) -> tuple[list[TranscriptValidationRun], list[TranscriptValidationRun]]:
    attributed: list[TranscriptValidationRun] = []
    ambiguous: list[TranscriptValidationRun] = []
    for run in runs:
        segments = run.validation_segments
        parsed_command = parse_shell_command(run.command)
        if run.outcome != "failure" or not parsed_command.operators:
            attributed.append(run)
            continue
        failure_categories = infer_failure_categories(run.output)
        candidates = [
            index
            for index, segment in enumerate(segments)
            if failure_categories.intersection(segment.categories)
        ]
        if len(candidates) != 1:
            ambiguous.append(run)
            continue
        failed_index = candidates[0]
        preceding_segments_succeeded = set(parsed_command.operators) == {"&&"}
        for index, segment in enumerate(segments):
            outcome: EvidenceOutcome
            if index == failed_index:
                outcome = "failure"
            elif preceding_segments_succeeded and index < failed_index:
                outcome = "success"
            else:
                continue
            attributed.append(
                replace(
                    run,
                    command=segment.command,
                    categories=segment.categories,
                    outcome=outcome,
                    exit_code=run.exit_code
                    if outcome == "failure"
                    else 0
                    if outcome == "success"
                    else None,
                    unknown_reason=None,
                    validation_segments=(segment,),
                )
            )
    unresolved_ambiguous = [
        run for run in ambiguous if not _later_successes_cover_every_segment(run, attributed)
    ]
    return attributed, unresolved_ambiguous


def _later_successes_cover_every_segment(
    failure: TranscriptValidationRun,
    runs: Iterable[TranscriptValidationRun],
) -> bool:
    required_commands = {segment.command for segment in failure.validation_segments}
    if not required_commands:
        return False
    successful_commands: set[str] = set()
    failure_order = (failure.completed_at, failure.order)
    for run in runs:
        if run.outcome != "success" or (run.completed_at, run.order) <= failure_order:
            continue
        segments = run.validation_segments
        if not segments:
            successful_commands.add(run.command)
            continue
        if len(segments) == 1 or set(parse_shell_command(run.command).operators) == {"&&"}:
            successful_commands.update(segment.command for segment in segments)
    return required_commands.issubset(successful_commands)


def _validation_details(evidence: TranscriptEvidence) -> dict[str, Any]:
    last_edit_order = max((edit.order for edit in evidence.edits), default=None)
    unknown_count = sum(run.outcome == "unknown" for run in evidence.validation_runs)
    return {
        "sessions": list(evidence.sessions),
        "validation_run_count": len(evidence.validation_runs),
        "unknown_outcome_count": unknown_count,
        "last_task_edit_order": last_edit_order,
        "degraded_capabilities": list(evidence.degraded_capabilities),
    }


def _degraded_message(evidence: TranscriptEvidence) -> str:
    if not evidence.degraded_capabilities:
        return ""
    capabilities = "; ".join(evidence.degraded_capabilities)
    return (
        f"Some transcript outcomes were unknown ({capabilities}); unknown results neither satisfy "
        "nor block the gate. Re-run the command so the provider records a definitive exit status."
    )


__all__ = [
    "CloseChecklist",
    "CloseGateResult",
    "GateStatus",
    "evaluate_validation_commands",
    "first_failed_gate",
]
