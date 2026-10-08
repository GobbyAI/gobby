"""Deterministic checklist facts used by the task-close lifecycle."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Literal

from gobby.config.shell_lexing import parse_shell_command
from gobby.tasks.close_test_coverage import (
    CloseCandidate,
    changed_python_source_paths,
    changed_web_source_paths,
    copy_differing_paths,
    coverage_failure_message,
    drop_foreign_runs,
    related_python_source_tests,
    uncovered_pytest_paths,
    uncovered_vitest_related_paths,
)
from gobby.tasks.close_test_coverage import (
    changed_python_test_paths as _changed_python_test_paths,
)
from gobby.tasks.close_test_coverage import pytest_module_paths as _pytest_module_paths
from gobby.tasks.close_test_coverage import (
    test_types_audit_targets as _test_types_audit_targets,
)
from gobby.tasks.close_test_coverage import (
    uncovered_test_paths as _uncovered_test_paths,
)
from gobby.tasks.criterion_commands import (
    SCOPE_MISMATCH_REASON,
    criterion_command_gap_message,
    criterion_command_records,
    edit_details,
    execution_details,
    expand_successful_and_segments,
    first_invalidating_edit,
    fit_sentences,
)
from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_outcomes import (
    EvidenceOutcome,
    infer_failure_categories,
)
from gobby.tasks.validation_diagnostics import (
    bound_diagnostic_record,
    bound_review_details,
    failure_command_description,
    unsatisfied_criterion_records,
)

GateStatus = Literal["passed", "failed", "skipped", "not_run"]

_TEST_REQUIRED_CATEGORIES = frozenset({"code", "refactor", "test"})
_AUTO_PASS_CATEGORIES = frozenset({"docs", "planning", "research", "manual"})
_OBSERVED_MESSAGE_BUDGET = 1_000
_TEST_TYPES_AUDIT_COMMAND = (
    "uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new"
)


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
    """Every deterministic gate result in checklist order."""

    gates: tuple[CloseGateResult, ...]

    @property
    def ready(self) -> bool:
        return all(gate.passed for gate in self.gates)

    @property
    def first_failure(self) -> CloseGateResult | None:
        return next((gate for gate in self.gates if not gate.passed), None)

    @property
    def all_failures(self) -> tuple[CloseGateResult, ...]:
        """Every failed gate, so one response names every blocker the caller must fix."""
        return tuple(gate for gate in self.gates if not gate.passed)

    def summary(self) -> list[dict[str, Any]]:
        """Gate statuses without the detail payloads, which run to tens of kilobytes."""
        return [
            {
                "item": gate.item,
                "name": gate.name,
                "status": gate.status,
                "message": gate.message,
            }
            for gate in self.gates
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "gates": [gate.to_dict() for gate in self.gates],
        }


def evaluate_validation_commands(
    *,
    task_category: str | None,
    evidence: TranscriptEvidence,
    has_attributed_edits: bool,
    validation_criteria: str = "",
    changed_paths: Iterable[str] = (),
    deleted_paths: Iterable[str] = (),
    close_root: str | None = None,
    candidate_commit_sha: str | None = None,
) -> CloseGateResult:
    """Keep credit decisions separate from observed-run explanations.

    A run whose ``uv`` location points outside ``close_root`` validated another
    checkout, so it neither credits nor fails this task, unless it ran identical
    copies of the changed tests. A copy must match ``candidate_commit_sha`` when given.
    """
    from gobby.tasks.validation_diagnostics import excluded_validation_records, observed_message

    paths = tuple(changed_paths)
    changed_tests = _changed_python_test_paths(paths)
    foreign: list[str] = []
    candidate: CloseCandidate | None = None
    if close_root is not None:
        candidate = (
            CloseCandidate(close_root, candidate_commit_sha) if candidate_commit_sha else None
        )
        evidence, foreign = drop_foreign_runs(evidence, changed_tests, close_root, paths, candidate)
    gate = _evaluate_validation_commands(
        task_category=task_category,
        evidence=evidence,
        has_attributed_edits=has_attributed_edits,
        validation_criteria=validation_criteria,
        changed_paths=paths,
        deleted_paths=deleted_paths,
        close_root=close_root,
        candidate=candidate,
    )

    def uncovered(run: TranscriptValidationRun) -> tuple[str, ...] | None:
        targets = _test_types_audit_targets(run)
        return _uncovered_test_paths(changed_tests, targets) if targets is not None else None

    records = excluded_validation_records(evidence, uncovered, tuple(changed_tests))
    gaps = gate.details.get("criterion_command_gaps", [])
    for gap in unsatisfied_criterion_records(gate.details):
        for observation in gap.get("observed_forms", []):
            if not str(observation.get("reason", "")).startswith(SCOPE_MISMATCH_REASON):
                continue
            if any(
                record["session_id"] == observation["session_id"]
                and record["order"] == observation["order"]
                for record in records
            ):
                continue
            records.append(
                {
                    **observation,
                    "reason_code": "partial",
                    "remedy": f"Run `{gap['command']}` clean after the final task edit, with all required flags and targets.",
                    "categories": [],
                }
            )
    records.sort(
        key=lambda record: (str(record["completed_at"]), int(record["order"])), reverse=True
    )
    relevant = records
    if gaps:
        from gobby.tasks.criterion_commands import _related_command

        relevant = [
            record
            for record in records
            if any(
                _related_command(record["command"], str(gap.get("core_command") or gap["command"]))
                for gap in gaps
            )
        ]
    elif changed_tests and (
        gate.details.get("latest_test_types_audit") is None
        or gate.details["latest_test_types_audit"]["outcome"] != "success"
    ):
        relevant = [record for record in records if "gobby test-types audit" in record["command"]]
    elif gate.details.get("unresolved_failure_categories"):
        relevant = [
            record
            for record in records
            if set(record["categories"]).intersection(gate.details["unresolved_failure_categories"])
        ]
    elif (
        gate.details.get("pytest_uncovered_paths")
        or gate.details.get("python_source_uncovered_tests")
        or gate.details.get("vitest_related_uncovered_paths")
    ):
        relevant = []
    elif task_category in _TEST_REQUIRED_CATEGORIES:
        relevant = [record for record in records if "test" in record["categories"]]
    nearest = relevant[0] if relevant else None
    excluded_runs = [bound_diagnostic_record(record) for record in records[:16]]
    nearest_observed = None if nearest is None else bound_diagnostic_record(nearest)
    details = {
        **gate.details,
        "excluded_runs": excluded_runs,
        "nearest_observed_run": nearest_observed,
        "foreign_scope_runs": [
            failure_command_description(command) for command in dict.fromkeys(foreign)
        ],
    }
    if len(records) > 16:
        details["omitted_excluded_run_count"] = len(records) - 16
    details = bound_review_details(details, validation_criteria)
    message = gate.message
    if gate.status == "failed" and gaps:
        messages = []
        for gap in gaps:
            required = str(gap.get("core_command") or gap["command"])
            observation = next(
                (record for record in relevant if _related_command(record["command"], required)),
                None,
            )
            if observation is not None:
                explanation = observed_message(bound_diagnostic_record(observation))
                messages.append(explanation.split("Run `", 1)[0].rstrip())
        kept, omitted, _ = fit_sentences(messages, _OBSERVED_MESSAGE_BUDGET)
        if omitted:
            kept.append(
                f"{omitted} more observed commands are omitted here; criterion_commands "
                "holds the full records."
            )
        message = " ".join([gate.message, *kept])
    elif gate.status == "failed" and nearest_observed is not None:
        message += " " + observed_message(nearest_observed)
    return replace(gate, message=message, details=details)


def _evaluate_validation_commands(
    *,
    task_category: str | None,
    evidence: TranscriptEvidence,
    has_attributed_edits: bool,
    validation_criteria: str = "",
    changed_paths: Iterable[str] = (),
    deleted_paths: Iterable[str] = (),
    close_root: str | None = None,
    candidate: CloseCandidate | None = None,
) -> CloseGateResult:
    """Evaluate checklist item 9 from transcript-derived validation commands.

    Unknown outcomes and wrapped successes are diagnostic only. Failed command
    sequences retain conservative per-segment failure attribution. A task-attributed
    edit makes every earlier run stale unless ``first_invalidating_edit`` shows the
    run's bounded inputs cannot read the edited file. Among credited fresh runs, the latest
    definitive outcome for each validation category wins, so a later clean run cures
    an earlier failure in the same category. ``latest_runs`` records the latest
    definitive run for each distinct core command so the criteria reviewer can treat
    them as the authoritative account of what ran. ``deleted_paths`` are tests that a
    linked commit deleted and HEAD no longer tracks: pytest cannot target them, so only
    the test type audit still has to cover them. Changed ``web/src`` files need a fresh
    passing ``vitest related`` run from ``web/`` naming each one; ``close_root`` resolves
    an absolute ``cd`` location.
    """
    category = (task_category or "").strip().casefold()
    changed_paths = tuple(changed_paths)
    deleted = frozenset(deleted_paths)
    changed_web_paths = changed_web_source_paths(
        path for path in changed_paths if path not in deleted
    )
    changed_python_test_paths = _changed_python_test_paths(changed_paths)
    source_tests = (
        related_python_source_tests(changed_paths, base_dir=close_root) if close_root else {}
    )
    # Rootless probes only inspect paths; selection runs in the rooted to_thread call.
    related_tests_required = (
        any(source_tests.values())
        if close_root
        else bool(changed_python_source_paths(changed_paths))
    )
    test_types_audit_required = bool(changed_python_test_paths)
    details = _validation_details(evidence)

    fresh_runs = _fresh_runs(evidence)
    definitive = [run for run in fresh_runs if run.outcome != "unknown"]
    credited = [run for run in definitive if not run.wrapped and run.core_command is not None]
    passing_runs = [run for run in credited if run.outcome == "success"]
    covers = partial(
        uncovered_pytest_paths,
        passing_runs,
        close_root=close_root,
        changed_paths=changed_paths,
        candidate=candidate,
    )
    uncovered_sources = {
        source: list(uncovered)
        for source, tests in source_tests.items()
        if (uncovered := covers(tests))
    }
    details.update(
        python_source_related_tests={source: list(tests) for source, tests in source_tests.items()},
        python_source_uncovered_tests=uncovered_sources,
        python_sources_without_related_tests=[
            source for source, tests in source_tests.items() if not tests
        ],
    )
    sequence_failures = [
        run
        for run in definitive
        if run.outcome == "failure" and run.wrapper_reason == "command sequence"
    ]
    attributed = _attribute_compound_failures((*credited, *sequence_failures))
    audit_coverage: list[tuple[TranscriptValidationRun, tuple[str, ...], tuple[str, ...]]] = []
    for run in credited:
        targets = _test_types_audit_targets(run)
        if targets is None:
            continue
        uncovered = _uncovered_test_paths(changed_python_test_paths, targets)
        audit_coverage.append((run, targets, uncovered))
    latest_audit_entry = max(
        (entry for entry in audit_coverage if not entry[2]),
        key=lambda entry: (entry[0].order, entry[0].completed_at),
        default=None,
    )
    latest_partial_entry = max(
        (entry for entry in audit_coverage if entry[2]),
        key=lambda entry: (entry[0].order, entry[0].completed_at),
        default=None,
    )
    latest_audit = latest_audit_entry[0] if latest_audit_entry is not None else None
    audit_targets: tuple[str, ...] = ()
    uncovered_test_paths = changed_python_test_paths
    if latest_partial_entry is not None:
        audit_targets = latest_partial_entry[1]
        uncovered_test_paths = latest_partial_entry[2]
    if latest_audit_entry is not None:
        audit_targets = latest_audit_entry[1]
        uncovered_test_paths = ()
    latest_by_category = _latest_definitive_by_category(attributed)
    latest_by_command: dict[str, TranscriptValidationRun] = {}
    command_evidence = expand_successful_and_segments(definitive)
    for run in sorted(command_evidence, key=lambda item: (item.order, item.completed_at)):
        command_key = run.core_command if run.core_command is not None else run.command
        latest_by_command[command_key] = run
    unresolved = {
        run_category: run
        for run_category, run in latest_by_category.items()
        if run.outcome == "failure"
    }
    unresolved_failures = [
        {
            "category": run_category,
            "command": failure_command_description(run.command),
            "completed_at": run.completed_at.isoformat(),
        }
        for run_category, run in sorted(unresolved.items())
    ]
    criterion_commands = criterion_command_records(validation_criteria, evidence)
    criterion_command_gaps = [record for record in criterion_commands if not record["satisfied"]]
    uncovered_web = uncovered_vitest_related_paths(
        passing_runs,
        changed_web_paths,
        close_root=close_root,
        changed_paths=changed_paths,
        candidate=candidate,
    )
    details = {
        **details,
        "fresh_run_count": len(fresh_runs),
        "latest_outcomes": {
            run_category: run.outcome for run_category, run in sorted(latest_by_category.items())
        },
        "latest_runs": [
            {
                "category": run.categories[0] if run.categories else None,
                "command": run.command,
                "core_command": run.core_command,
                "wrapped": run.wrapped,
                "completed_at": run.completed_at.isoformat(),
                "outcome": run.outcome,
                "exit_code": run.exit_code,
            }
            for run in sorted(
                latest_by_command.values(),
                key=lambda item: (item.completed_at, item.order, item.command),
            )
        ],
        "unresolved_failure_categories": sorted(unresolved),
        "unresolved_failures": unresolved_failures,
        "vitest_related_uncovered_paths": list(uncovered_web),
        "test_types_audit_required": test_types_audit_required,
        "changed_python_test_paths": list(changed_python_test_paths),
        "test_types_audit_targets": list(audit_targets),
        "test_types_audit_uncovered_paths": list(uncovered_test_paths),
        "canonical_test_types_audit_command": (
            _TEST_TYPES_AUDIT_COMMAND if test_types_audit_required else None
        ),
        "latest_test_types_audit": (
            {
                "command": latest_audit.command,
                "completed_at": latest_audit.completed_at.isoformat(),
                "outcome": latest_audit.outcome,
                "exit_code": latest_audit.exit_code,
            }
            if latest_audit is not None
            else None
        ),
        "criterion_commands": criterion_commands,
        # Compact references only: every gap's full record, including its observed
        # forms, is the matching unsatisfied entry in ``criterion_commands``.
        "criterion_command_gaps": [
            {
                "command": record["command"],
                "core_command": record["core_command"],
                "status": record["status"],
            }
            for record in criterion_command_gaps
        ],
    }

    if criterion_command_gaps:
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="failed",
            message=criterion_command_gap_message(criterion_command_gaps),
            details=details,
        )

    # Exempt tasks still need the command record for their explicit criteria review.
    if (
        not has_attributed_edits
        and not test_types_audit_required
        and not related_tests_required
        and not changed_web_paths
    ):
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="skipped",
            message="Validation command requirement skipped because the task has no attributed edits.",
            details={**details, "skip_reason": "no-edit"},
        )

    if (
        category in _AUTO_PASS_CATEGORIES
        and not test_types_audit_required
        and not related_tests_required
        and not changed_web_paths
    ):
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="skipped",
            message=f"Validation command requirement skipped for task category '{category}'.",
            details={**details, "skip_reason": "category"},
        )

    if test_types_audit_required and latest_audit is None:
        if latest_partial_entry is not None:
            uncovered_display = ", ".join(f"`{path}`" for path in uncovered_test_paths)
            return CloseGateResult(
                item=9,
                name="validation_commands",
                status="failed",
                message=(
                    "The latest Python test type audit did not cover every changed Python test. "
                    f"Uncovered paths: {uncovered_display}. Run `{_TEST_TYPES_AUDIT_COMMAND}` clean "
                    "after the final task edit, or audit explicit targets covering every listed path."
                ),
                details=details,
            )
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="failed",
            message=(
                "The required Python test type audit has no credited run. "
                f"Run `{_TEST_TYPES_AUDIT_COMMAND}` clean after the final task edit."
            ),
            details=details,
        )

    if test_types_audit_required and latest_audit is not None and latest_audit.outcome != "success":
        reason = f"last failed at {latest_audit.completed_at.isoformat()}"
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="failed",
            message=(
                f"The required Python test type audit {reason}. "
                f"Run `{_TEST_TYPES_AUDIT_COMMAND}` clean after the final task edit."
            ),
            details=details,
        )

    if unresolved:
        blockers = [
            f"{failure['category']}: {failure['command']!r} at {failure['completed_at']}"
            for failure in unresolved_failures
        ]
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="failed",
            message=(
                f"A validation command is still failing ({'; '.join(blockers)}). "
                "Re-run each category clean after the final task edit."
            ),
            details=details,
        )

    pytest_required_paths = _pytest_module_paths(
        path for path in changed_python_test_paths if path not in deleted
    )
    details["pytest_exempt_deleted_paths"] = [
        path for path in changed_python_test_paths if path in deleted
    ]
    uncovered_pytest: tuple[str, ...] = ()
    if pytest_required_paths:
        uncovered_pytest = covers(pytest_required_paths)
        details["pytest_uncovered_paths"] = list(uncovered_pytest)
    declined = (*uncovered_pytest, *(t for tests in uncovered_sources.values() for t in tests))
    if differing := copy_differing_paths(
        passing_runs, declined, close_root, changed_paths, candidate
    ):
        details["pytest_copy_differing_paths"] = list(differing)
    coverage_failure = coverage_failure_message(
        uncovered_pytest,
        uncovered_sources,
        uncovered_web,
        differing_paths=differing,
        close_root=close_root,
    )
    if coverage_failure:
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="failed",
            message=coverage_failure,
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

    if has_success or category in _AUTO_PASS_CATEGORIES:
        message = "A clean validation command ran after the final task edit."
        if required_category:
            message = "A clean test-category validation command ran after the final task edit."
        elif test_types_audit_required:
            message = (
                "The required Python test type audit covered every changed test and ran clean."
            )
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
    return [
        run
        for run in (*evidence.validation_runs, *evidence.command_runs)
        if first_invalidating_edit(evidence, run) is None
    ]


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
) -> list[TranscriptValidationRun]:
    """Split each compound failure into one run per validation segment.

    When the output names exactly one failing segment, that segment fails and,
    in an ``&&`` chain, the segments before it passed. Otherwise every
    validation segment inherits the failure: the transcript cannot prove that
    any of them passed, so each category needs its own later clean run, the
    same cure an attributed failure requires.
    """
    attributed: list[TranscriptValidationRun] = []
    for run in runs:
        segments = run.validation_segments
        parsed_command = parse_shell_command(run.command)
        if run.outcome != "failure" or not parsed_command.operators or not segments:
            attributed.append(run)
            continue
        failure_categories = infer_failure_categories(run.output)
        candidates = [
            index
            for index, segment in enumerate(segments)
            if failure_categories.intersection(segment.categories)
        ]
        if len(candidates) == 1:
            failed_indexes = {candidates[0]}
            passed_indexes = (
                set(range(candidates[0])) if set(parsed_command.operators) == {"&&"} else set()
            )
        else:
            failed_indexes = set(range(len(segments)))
            passed_indexes = set()
        for index, segment in enumerate(segments):
            outcome: EvidenceOutcome
            if index in failed_indexes:
                outcome = "failure"
            elif index in passed_indexes:
                outcome = "success"
            else:
                continue
            attributed.append(
                replace(
                    run,
                    command=segment.command,
                    categories=segment.categories,
                    outcome=outcome,
                    exit_code=run.exit_code if outcome == "failure" else 0,
                    unknown_reason=None,
                    validation_segments=(segment,),
                )
            )
    return attributed


def _validation_details(evidence: TranscriptEvidence) -> dict[str, Any]:
    last_edit_order = max((edit.order for edit in evidence.edits), default=None)
    unknown_count = sum(
        run.outcome == "unknown" for run in (*evidence.validation_runs, *evidence.command_runs)
    )
    return {
        "sessions": list(evidence.sessions),
        "validation_run_count": len(evidence.validation_runs),
        "command_run_count": len(evidence.command_runs),
        "unknown_outcome_count": unknown_count,
        "uncredited_runs": _uncredited_runs(evidence),
        "last_task_edit_order": last_edit_order,
        "degraded_capabilities": list(evidence.degraded_capabilities),
    }


def _uncredited_runs(evidence: TranscriptEvidence) -> list[dict[str, object]]:
    uncredited: list[dict[str, object]] = []
    runs = sorted(
        (*evidence.validation_runs, *evidence.command_runs),
        key=lambda item: (item.order, item.completed_at),
    )
    staleness = [(run, first_invalidating_edit(evidence, run)) for run in runs]
    fresh_success_cores = {
        run.core_command
        for run, invalidating_edit in staleness
        if invalidating_edit is None
        and run.outcome == "success"
        and not run.wrapped
        and run.core_command is not None
    }
    for run, invalidating_edit in staleness:
        if invalidating_edit is not None:
            if run.core_command in fresh_success_cores:
                continue
            uncredited.append(
                {
                    **execution_details(run),
                    "reason": "stale after a later task edit",
                    "invalidating_edit": edit_details(invalidating_edit),
                }
            )
        elif run.wrapped:
            uncredited.append(
                {
                    "command": run.command,
                    "reason": "wrapped",
                    "wrapper_reason": run.wrapper_reason,
                }
            )
        elif run.outcome == "unknown":
            uncredited.append({"command": run.command, "reason": "unknown outcome"})
    return uncredited


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
]
