"""Deterministic checklist facts used by the task-close lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from gobby.config.shell_lexing import parse_shell_command, safe_split
from gobby.tasks.transcript_evidence import TranscriptEvidence, TranscriptValidationRun
from gobby.tasks.transcript_outcomes import (
    EvidenceOutcome,
    classify_validation_command_equivalence,
    infer_failure_categories,
)

GateStatus = Literal["passed", "failed", "skipped"]

_TEST_REQUIRED_CATEGORIES = frozenset({"code", "refactor", "test"})
_AUTO_PASS_CATEGORIES = frozenset({"docs", "planning", "research", "manual"})
_REVIEW_COMMAND_BUDGET = 48_000
_REVIEW_COMMAND_LIMIT = 64
_DIAGNOSTIC_COMMAND_LIMIT = 2_048
_TEST_TYPES_AUDIT_MATCHER = "gobby-test-types-audit"
_TEST_TYPES_AUDIT_COMMAND = (
    "uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new"
)
_TEST_TYPES_BASELINE = ".gobby/test-types-baseline.json"
_GENERIC_COMMAND_WORDS = frozenset(
    {"uv", "run", "npx", "npm", "python", "python3", "bash", "sh", "git", "check", "test", "ci"}
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
    validation_criteria: str = "",
    changed_paths: Iterable[str] = (),
) -> CloseGateResult:
    """Evaluate checklist item 9 from transcript-derived validation commands.

    Unknown outcomes and wrapped successes are diagnostic only. Failed command
    sequences retain conservative per-segment failure attribution. A task-attributed
    edit makes every earlier run stale. Among credited fresh runs, the latest
    definitive outcome for each validation category wins, so a later clean run cures
    an earlier failure in the same category. ``latest_runs`` records the latest
    definitive run for each distinct core command so the criteria reviewer can treat
    them as the authoritative account of what ran.
    """
    category = (task_category or "").strip().casefold()
    test_types_audit_required = _changed_python_tests(changed_paths)
    details = _validation_details(evidence)

    fresh_runs = _fresh_runs(evidence)
    definitive = [run for run in fresh_runs if run.outcome != "unknown"]
    credited = [run for run in definitive if not run.wrapped and run.core_command is not None]
    sequence_failures = [
        run
        for run in definitive
        if run.outcome == "failure" and run.wrapper_reason == "command sequence"
    ]
    attributed = _attribute_compound_failures((*credited, *sequence_failures))
    canonical_audits = [run for run in credited if _is_canonical_test_types_audit(run)]
    latest_audit = max(
        canonical_audits,
        key=lambda run: (run.order, run.completed_at),
        default=None,
    )
    latest_by_category = _latest_definitive_by_category(attributed)
    latest_by_command: dict[str, TranscriptValidationRun] = {}
    command_evidence = _expand_successful_and_segments(definitive)
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
            "command": _failure_command_description(run.command),
            "completed_at": run.completed_at.isoformat(),
        }
        for run_category, run in sorted(unresolved.items())
    ]
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
        "test_types_audit_required": test_types_audit_required,
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
    }
    details = _bound_review_details(details, validation_criteria)

    # Exempt tasks still need the command record for their explicit criteria review.
    if not has_attributed_edits and not test_types_audit_required:
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="skipped",
            message="Validation command requirement skipped because the task has no attributed edits.",
            details={**details, "skip_reason": "no-edit"},
        )

    if category in _AUTO_PASS_CATEGORIES and not test_types_audit_required:
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="skipped",
            message=f"Validation command requirement skipped for task category '{category}'.",
            details={**details, "skip_reason": "category"},
        )

    if test_types_audit_required and (latest_audit is None or latest_audit.outcome != "success"):
        reason = "is missing"
        if latest_audit is not None:
            reason = f"last failed at {latest_audit.completed_at.isoformat()}"
        return CloseGateResult(
            item=9,
            name="validation_commands",
            status="failed",
            message=(
                f"The required whole-tree Python test type audit {reason}. "
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
            message = "The required whole-tree Python test type audit ran clean."
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


def _changed_python_tests(changed_paths: Iterable[str]) -> bool:
    for path in changed_paths:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.startswith("tests/") and normalized.endswith(".py"):
            return True
    return False


def _is_canonical_test_types_audit(run: TranscriptValidationRun) -> bool:
    if run.matcher_id != _TEST_TYPES_AUDIT_MATCHER:
        return False
    core_command = run.core_command or run.command
    if len(parse_shell_command(core_command).segments) != 1:
        return False
    commands = [segment.command for segment in run.validation_segments]
    if not commands:
        commands = [core_command]
    return any(_is_canonical_test_types_command(command) for command in commands)


def _is_canonical_test_types_command(command: str) -> bool:
    tokens = safe_split(command)
    prefix = ["gobby", "test-types", "audit"]
    try:
        start = next(
            index
            for index in range(len(tokens) - len(prefix) + 1)
            if tokens[index : index + len(prefix)] == prefix
        )
    except StopIteration:
        return False

    arguments = tokens[start + len(prefix) :]
    targets: list[str] = []
    baselines: list[str] = []
    fail_on_new = 0
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--baseline":
            index += 1
            if index >= len(arguments):
                return False
            baselines.append(arguments[index])
        elif argument.startswith("--baseline="):
            baselines.append(argument.partition("=")[2])
        elif argument == "--fail-on-new":
            fail_on_new += 1
        else:
            targets.append(argument)
        index += 1
    return (
        targets in (["tests"], ["tests/"])
        and baselines == [_TEST_TYPES_BASELINE]
        and fail_on_new == 1
    )


def _fresh_runs(evidence: TranscriptEvidence) -> list[TranscriptValidationRun]:
    runs = [*evidence.validation_runs, *evidence.command_runs]
    if not evidence.edits:
        return runs
    last_edit_order = max(edit.order for edit in evidence.edits)
    return [run for run in runs if run.order > last_edit_order]


def _failure_command_description(command: str) -> str:
    """Keep failure diagnostics bounded without representing an excerpt as an exact run."""
    if len(command) <= _DIAGNOSTIC_COMMAND_LIMIT:
        return command
    digest = hashlib.sha256(command.encode()).hexdigest()
    return f"{command[:256]}… [command excerpt; sha256={digest}]"


def _command_priority(record: Mapping[str, object], criteria: str) -> int:
    """Prefer exact raw/core commands, then invoked tools and explicit path arguments.

    Inline script contents and ordinary prose are never command-name evidence.
    This affects selection only; the original equivalence/outcome still controls credit.
    """
    command = str(record["command"])
    core = record.get("core_command")
    if not isinstance(core, str):
        core = classify_validation_command_equivalence(command).core_command
    criteria = criteria.casefold()
    if any(value and value.casefold().strip() in criteria for value in (command, core)):
        return 3
    criterion_words = {word.rstrip(".") for word in re.findall(r"[\w./-]+", criteria)}
    priority = 0
    for segment in parse_shell_command(core or command).segments:
        arguments = list(segment)
        while arguments and arguments[0] in {"uv", "run", "npx", "--yes", "-y"}:
            arguments.pop(0)
        if not arguments:
            continue
        executable = arguments[0].rsplit("/", 1)[-1].casefold()
        if executable in {"python", "python3"} and arguments[1:2] == ["-m"]:
            arguments = arguments[2:]
            if not arguments:
                continue
            executable = arguments[0].casefold()
        if executable in {"python", "python3", "bash", "sh", "zsh", "echo", "printf"}:
            continue
        if executable not in _GENERIC_COMMAND_WORDS and executable in criterion_words:
            priority = max(priority, 2)
        if len(arguments) > 1 and " ".join(arguments[:2]).casefold() in criteria:
            priority = max(priority, 2)
        if any(
            argument.casefold() in criterion_words and ("/" in argument or "." in argument)
            for argument in arguments[1:]
        ):
            priority = max(priority, 1)
    return priority


def _select_command_records(
    records: list[dict[str, object]],
    *,
    priorities: Mapping[int, int],
    budget: int,
    limit: int,
    priority: int,
) -> tuple[list[dict[str, object]], int]:
    selected: list[tuple[int, dict[str, object]]] = []
    for index, record in reversed(list(enumerate(records))):
        command = str(record["command"])
        if priorities[id(record)] != priority:
            continue
        if len(command) > _DIAGNOSTIC_COMMAND_LIMIT and priority < 3:
            continue
        size = len(json.dumps(record, separators=(",", ":")))
        if size > budget or len(selected) >= limit:
            continue
        selected.append((index, record))
        budget -= size
    return [record for _, record in sorted(selected)], budget


def _bound_review_details(details: dict[str, Any], criteria: str) -> dict[str, Any]:
    """Bound presentation only; gate decisions retain all definitive category outcomes.

    Complete referenced commands are selected first. Never shorten a credited
    command: an omitted script cannot accidentally compare equal to a criterion.
    """
    records: dict[str, list[dict[str, object]]] = {
        "latest_runs": details["latest_runs"],
        "uncredited_runs": details["uncredited_runs"],
    }
    selected: dict[str, list[dict[str, object]]] = {key: [] for key in records}
    priorities = {
        id(record): _command_priority(record, criteria)
        for entries in records.values()
        for record in entries
    }
    remaining = _REVIEW_COMMAND_BUDGET
    for priority in (3, 2, 1, 0):
        for key, limit in (("latest_runs", _REVIEW_COMMAND_LIMIT), ("uncredited_runs", 16)):
            additions, remaining = _select_command_records(
                records[key],
                priorities=priorities,
                budget=remaining,
                limit=limit - len(selected[key]),
                priority=priority,
            )
            selected[key].extend(additions)
    for key, entries in records.items():
        selected_ids = {id(record) for record in selected[key]}
        details[key] = [record for record in entries if id(record) in selected_ids]
    omitted_latest = len(records["latest_runs"]) - len(details["latest_runs"])
    omitted_uncredited = len(records["uncredited_runs"]) - len(details["uncredited_runs"])
    if omitted_latest or omitted_uncredited:
        details.update(
            {
                "omitted_latest_run_count": omitted_latest,
                "omitted_uncredited_run_count": omitted_uncredited,
                "evidence_selection": (
                    "Bounded task-command evidence: referenced commands precede recent diagnostics. "
                    "Omitted commands are not proof of failure or absence. Only complete listed "
                    "commands support exact-command credit; category outcomes use all fresh runs."
                ),
            }
        )
    return details


def _latest_definitive_by_category(
    runs: Iterable[TranscriptValidationRun],
) -> dict[str, TranscriptValidationRun]:
    latest: dict[str, TranscriptValidationRun] = {}
    for run in sorted(runs, key=lambda item: (item.order, item.completed_at)):
        for category in run.categories:
            latest[category] = run
    return latest


def _expand_successful_and_segments(
    runs: Iterable[TranscriptValidationRun],
) -> list[TranscriptValidationRun]:
    """Expose each proven validation segment of a successful top-level ``&&`` chain."""
    expanded: list[TranscriptValidationRun] = []
    for run in runs:
        segments = run.validation_segments
        parsed = parse_shell_command(run.command)
        segment_indexes = [segment.segment_index for segment in segments]
        if (
            run.outcome != "success"
            or run.wrapped
            or not parsed.operators
            or set(parsed.operators) != {"&&"}
            or not segments
            or len(set(segment_indexes)) != len(segment_indexes)
            or any(not 0 <= index < len(parsed.segments) for index in segment_indexes)
            or len(parsed.segments) != len(parsed.operators) + 1
        ):
            expanded.append(run)
            continue
        segment_runs = [
            replace(
                run,
                command=shlex.join(parsed.segments[segment.segment_index]),
                categories=segment.categories,
                validation_segments=(segment,),
            )
            for segment in segments
        ]
        if any(
            segment_run.wrapped or segment_run.core_command is None for segment_run in segment_runs
        ):
            expanded.append(run)
            continue
        expanded.extend(segment_runs)
    return expanded


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
        "uncredited_runs": _uncredited_runs(evidence, last_edit_order),
        "last_task_edit_order": last_edit_order,
        "degraded_capabilities": list(evidence.degraded_capabilities),
    }


def _uncredited_runs(
    evidence: TranscriptEvidence,
    last_edit_order: int | None,
) -> list[dict[str, object]]:
    uncredited: list[dict[str, object]] = []
    for run in sorted(
        (*evidence.validation_runs, *evidence.command_runs),
        key=lambda item: (item.order, item.completed_at),
    ):
        if last_edit_order is not None and run.order <= last_edit_order:
            uncredited.append({"command": run.command, "reason": "stale after a later task edit"})
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
    "first_failed_gate",
]
