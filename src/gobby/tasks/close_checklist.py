"""Deterministic checklist facts used by the task-close lifecycle."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from gobby.config.shell_lexing import parse_shell_command, safe_split
from gobby.tasks.criterion_commands import (
    SCOPE_MISMATCH_REASON,
    criterion_command_gap_message,
    criterion_command_records,
    edit_details,
    execution_details,
    expand_successful_and_segments,
    first_invalidating_edit,
)
from gobby.tasks.transcript_evidence import (
    TranscriptEvidence,
    TranscriptValidationRun,
)
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
_CRITERIA_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")


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
    """Keep credit decisions separate from observed-run explanations."""
    from gobby.tasks.validation_diagnostics import excluded_validation_records, observed_message

    paths = tuple(changed_paths)
    gate = _evaluate_validation_commands(
        task_category=task_category,
        evidence=evidence,
        has_attributed_edits=has_attributed_edits,
        validation_criteria=validation_criteria,
        changed_paths=paths,
    )
    changed_tests = _changed_python_test_paths(paths)

    def uncovered(run: TranscriptValidationRun) -> tuple[str, ...] | None:
        targets = _test_types_audit_targets(run)
        return _uncovered_test_paths(changed_tests, targets) if targets is not None else None

    records = excluded_validation_records(evidence, uncovered, tuple(changed_tests))
    gaps = gate.details.get("criterion_command_gaps", [])
    for gap in gaps:
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
    elif task_category in _TEST_REQUIRED_CATEGORIES:
        relevant = [record for record in records if "test" in record["categories"]]
    nearest = relevant[0] if relevant else None
    excluded_runs = [_bound_diagnostic_record(record) for record in records[:16]]
    nearest_observed = None if nearest is None else _bound_diagnostic_record(nearest)
    details = {
        **gate.details,
        "excluded_runs": excluded_runs,
        "nearest_observed_run": nearest_observed,
    }
    if len(records) > 16:
        details["omitted_excluded_run_count"] = len(records) - 16
    details = _bound_review_details(details, validation_criteria)
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
                explanation = observed_message(_bound_diagnostic_record(observation))
                messages.append(explanation.split("Run `", 1)[0].rstrip())
        message = gate.message + " " + " ".join(messages)
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
) -> CloseGateResult:
    """Evaluate checklist item 9 from transcript-derived validation commands.

    Unknown outcomes and wrapped successes are diagnostic only. Failed command
    sequences retain conservative per-segment failure attribution. A task-attributed
    edit makes every earlier run stale unless ``first_invalidating_edit`` shows the
    run's bounded inputs cannot read the edited file. Among credited fresh runs, the latest
    definitive outcome for each validation category wins, so a later clean run cures
    an earlier failure in the same category. ``latest_runs`` records the latest
    definitive run for each distinct core command so the criteria reviewer can treat
    them as the authoritative account of what ran.
    """
    category = (task_category or "").strip().casefold()
    changed_python_test_paths = _changed_python_test_paths(changed_paths)
    test_types_audit_required = bool(changed_python_test_paths)
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
            "command": _failure_command_description(run.command),
            "completed_at": run.completed_at.isoformat(),
        }
        for run_category, run in sorted(unresolved.items())
    ]
    criterion_commands = criterion_command_records(validation_criteria, evidence)
    criterion_command_gaps = [record for record in criterion_commands if not record["satisfied"]]
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
        "criterion_command_gaps": criterion_command_gaps,
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


def _changed_python_test_paths(changed_paths: Iterable[str]) -> tuple[str, ...]:
    python_tests: set[str] = set()
    for path in changed_paths:
        normalized = _normalize_repo_path(path)
        if (
            normalized is not None
            and normalized.startswith("tests/")
            and normalized.endswith(".py")
        ):
            python_tests.add(normalized)
    return tuple(sorted(python_tests))


def _test_types_audit_targets(run: TranscriptValidationRun) -> tuple[str, ...] | None:
    if run.matcher_id != _TEST_TYPES_AUDIT_MATCHER:
        return None
    core_command = run.core_command or run.command
    if len(parse_shell_command(core_command).segments) != 1:
        return None
    commands = [segment.command for segment in run.validation_segments]
    if not commands:
        commands = [core_command]
    for command in commands:
        targets = _test_types_command_targets(command)
        if targets is not None:
            return targets
    return None


def _test_types_command_targets(command: str) -> tuple[str, ...] | None:
    tokens = safe_split(command)
    prefix = ["gobby", "test-types", "audit"]
    try:
        start = next(
            index
            for index in range(len(tokens) - len(prefix) + 1)
            if tokens[index : index + len(prefix)] == prefix
        )
    except StopIteration:
        return None

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
                return None
            baselines.append(arguments[index])
        elif argument.startswith("--baseline="):
            baselines.append(argument.partition("=")[2])
        elif argument == "--fail-on-new":
            fail_on_new += 1
        elif argument.startswith("-"):
            return None
        else:
            targets.append(argument)
        index += 1
    if baselines != [_TEST_TYPES_BASELINE] or fail_on_new != 1 or not targets:
        return None
    normalized_targets: list[str] = []
    for target in targets:
        normalized = _normalize_repo_path(target)
        if normalized is None:
            return None
        normalized_targets.append(normalized)
    return tuple(normalized_targets)


def _normalize_repo_path(path: str) -> str | None:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def _uncovered_test_paths(
    changed_python_tests: tuple[str, ...],
    audit_targets: tuple[str, ...],
) -> tuple[str, ...]:
    """Return changed tests outside every lexical target, including missing paths."""
    return tuple(
        path
        for path in changed_python_tests
        if not any(
            target == "." or path == target or path.startswith(f"{target}/")
            for target in audit_targets
        )
    )


def _fresh_runs(evidence: TranscriptEvidence) -> list[TranscriptValidationRun]:
    return [
        run
        for run in (*evidence.validation_runs, *evidence.command_runs)
        if first_invalidating_edit(evidence, run) is None
    ]


def _failure_command_description(command: str) -> str:
    """Keep failure diagnostics bounded without representing an excerpt as an exact run."""
    if len(command) <= _DIAGNOSTIC_COMMAND_LIMIT:
        return command
    digest = hashlib.sha256(command.encode()).hexdigest()
    return f"{command[:256]}… [command excerpt; sha256={digest}]"


def _bound_diagnostic_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Clamp diagnostic command text; keep reason, remedy, and invalidating_edit."""
    bounded = dict(record)
    command = str(bounded.get("command") or "")
    bounded_command = _failure_command_description(command)
    bounded["command"] = bounded_command
    core = bounded.get("core_command")
    if core == command:
        del bounded["core_command"]
    elif isinstance(core, str):
        bounded_core = _failure_command_description(core)
        bounded["core_command"] = bounded_core
        if bounded_core != core:
            for key in ("reason", "remedy"):
                value = bounded.get(key)
                if isinstance(value, str) and core in value:
                    bounded[key] = value.replace(core, bounded_core)
    if bounded_command != command:
        for key in ("reason", "remedy"):
            value = bounded.get(key)
            if isinstance(value, str) and command in value:
                bounded[key] = value.replace(command, bounded_command)
    return bounded


def _command_priority(
    record: Mapping[str, object],
    criteria: str,
    criterion_cores: frozenset[str],
) -> int:
    """Prefer exact raw/core commands, then invoked tools and explicit path arguments.

    Inline script contents and ordinary prose are never command-name evidence.
    This affects selection only; the original equivalence/outcome still controls credit.
    """
    command = str(record["command"])
    core = record.get("core_command")
    if not isinstance(core, str):
        core = classify_validation_command_equivalence(command).core_command
    criteria = criteria.casefold()
    if core is not None and core.casefold() in criterion_cores:
        return 3
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
    limits: dict[str, int] = {
        "latest_runs": _REVIEW_COMMAND_LIMIT,
        "uncredited_runs": 16,
    }
    if "excluded_runs" in details:
        records["excluded_runs"] = details["excluded_runs"]
        limits["excluded_runs"] = 16
    selected: dict[str, list[dict[str, object]]] = {key: [] for key in records}
    criterion_cores = frozenset(
        equivalence.core_command.casefold()
        for match in _CRITERIA_CODE_SPAN_RE.finditer(criteria)
        if (
            equivalence := classify_validation_command_equivalence(match.group(1).strip())
        ).core_command
    )
    priorities = {
        id(record): _command_priority(record, criteria, criterion_cores)
        for entries in records.values()
        for record in entries
    }
    remaining = _REVIEW_COMMAND_BUDGET
    key_order = [
        key for key in ("excluded_runs", "latest_runs", "uncredited_runs") if key in records
    ]
    for priority in (3, 2, 1, 0):
        for key in key_order:
            additions, remaining = _select_command_records(
                records[key],
                priorities=priorities,
                budget=remaining,
                limit=limits[key] - len(selected[key]),
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
    if "excluded_runs" in records:
        omitted_excluded = len(records["excluded_runs"]) - len(details["excluded_runs"])
        if omitted_excluded:
            details["omitted_excluded_run_count"] = (
                int(details.get("omitted_excluded_run_count") or 0) + omitted_excluded
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
    "first_failed_gate",
]
