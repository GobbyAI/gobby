"""Diagnostic explanations and bounded presentation of task validation evidence."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Callable, Mapping
from typing import Any

from gobby.config.shell_lexing import parse_shell_command
from gobby.tasks.criterion_commands import edit_details, execution_details, first_invalidating_edit
from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_outcomes import classify_validation_command_equivalence


def excluded_validation_records(
    evidence: TranscriptEvidence,
    uncovered_paths: Callable[[TranscriptValidationRun], tuple[str, ...] | None],
    audit_paths: tuple[str, ...] = ("tests/",),
) -> list[dict[str, Any]]:
    """Describe exclusions without adding observations to credit-bearing evidence."""
    records: list[dict[str, Any]] = []
    for prelink, runs in (
        (False, (*evidence.validation_runs, *evidence.command_runs)),
        (True, evidence.excluded_runs),
    ):
        for run in runs:
            retry = run.core_command or (None if run.wrapped else run.command)
            rerun = (
                f"Run `{retry}` directly and clean after the final task edit."
                if retry
                else "Run the exact required validation command directly and clean after the final task edit."
            )
            record = execution_details(run)
            missing = uncovered_paths(run)
            if prelink:
                code, reason = "pre-link", "Observed before the task/session link window"
                remedy = f"Link this session to the task, then rerun; prior runs remain uncredited. {rerun}"
            elif (edit := first_invalidating_edit(evidence, run)) is not None:
                code, reason = "stale", "Invalidated by a later task edit"
                record["invalidating_edit"] = edit_details(edit)
                reason += f" to {edit.path} at {edit.timestamp.isoformat()}"
                remedy = rerun
            elif run.wrapped:
                code, reason = (
                    "wrapped",
                    f"Wrapped execution: {run.wrapper_reason or 'shell wrapper'}",
                )
                remedy = (
                    "Remove pipes, redirects, wrappers, and trailing commands; run each validation command directly. "
                    + rerun
                )
            elif run.outcome == "unknown":
                code, reason = (
                    "unknown",
                    run.unknown_reason or "No definitive exit outcome was observed",
                )
                remedy = (
                    "Finish the command and capture its definitive exit status using a supported shell tool. "
                    + rerun
                )
            elif run.outcome == "failure":
                code, reason, remedy = failed_execution(run, rerun)
            elif missing:
                code, reason = "partial", "Audit did not cover: " + ", ".join(missing)
                record["uncovered_paths"] = list(missing)
                remedy = (
                    "Run `uv run gobby test-types audit "
                    + shlex.join(audit_paths)
                    + " --baseline .gobby/test-types-baseline.json --fail-on-new` clean after the final task edit."
                    " The audit cannot read a deleted test file; target its parent directory instead."
                )
            elif not run.categories or missing is None and "gobby test-types audit" in run.command:
                code, reason = (
                    "unknown",
                    "Command or audit arguments do not match the validation policy",
                )
                remedy = "Run the exact required command, with its required flags and targets, directly after the final task edit."
            else:
                continue
            records.append(
                {
                    **record,
                    "reason_code": code,
                    "reason": reason,
                    "remedy": remedy,
                    "categories": list(run.categories),
                }
            )
    records.sort(
        key=lambda record: (str(record["completed_at"]), int(record["order"])), reverse=True
    )
    return records


def failed_execution(run: TranscriptValidationRun, rerun: str) -> tuple[str, str, str]:
    """Only classify a definitive direct execution, never a compound segment."""
    if run.exit_code in {126, 127}:
        return (
            "invocation-failed",
            f"Command could not be invoked (exit {run.exit_code})",
            "Fix the executable, permissions, or command path. " + rerun,
        )
    if "pytest" in (run.core_command or run.command) and run.exit_code in {2, 3, 4, 5}:
        reasons = {
            2: "Pytest interrupted or failed during collection",
            3: "Pytest internal error",
            4: "Pytest command-line/usage error",
            5: "Pytest collected no tests",
        }
        return (
            "invocation-failed",
            f"{reasons[run.exit_code]} (exit {run.exit_code})",
            "Fix collection, invocation arguments, or test selection before rerunning. " + rerun,
        )
    if run.exit_code == 1 and run.categories:
        return (
            "assertion-failed",
            "Validation assertions or findings failed (exit 1)",
            "Fix the reported assertions or validation findings. " + rerun,
        )
    return (
        "unknown",
        f"Validation failed with exit {run.exit_code}; failure stage is unknown",
        "Inspect the command error and fix its cause. " + rerun,
    )


def observed_message(record: Mapping[str, Any]) -> str:
    return (
        f"Observed `{record['command']}` at {record['completed_at']}: "
        f"{record['reason']} ({record['reason_code']}). {record['remedy']}"
    )


# Character budget shared by every bounded evidence section of one gate-10 record.
REVIEW_COMMAND_BUDGET = 48_000
_REVIEW_COMMAND_LIMIT = 64
_DIAGNOSTIC_COMMAND_LIMIT = 2_048
_GENERIC_COMMAND_WORDS = frozenset(
    {"uv", "run", "npx", "npm", "python", "python3", "bash", "sh", "git", "check", "test", "ci"}
)
_CRITERIA_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")


def failure_command_description(command: str) -> str:
    """Keep failure diagnostics bounded without representing an excerpt as an exact run."""
    if len(command) <= _DIAGNOSTIC_COMMAND_LIMIT:
        return command
    digest = hashlib.sha256(command.encode()).hexdigest()
    return f"{command[:256]}… [command excerpt; sha256={digest}]"


def bound_diagnostic_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Clamp diagnostic command text; keep reason, remedy, and invalidating_edit."""
    bounded = dict(record)
    command = str(bounded.get("command") or "")
    bounded_command = failure_command_description(command)
    bounded["command"] = bounded_command
    core = bounded.get("core_command")
    if core == command:
        del bounded["core_command"]
    elif isinstance(core, str):
        bounded_core = failure_command_description(core)
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


def unsatisfied_criterion_records(details: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the full record behind each compact ``criterion_command_gaps`` entry."""
    records = details.get("criterion_commands")
    if not isinstance(records, list):
        return []
    return [record for record in records if not record.get("satisfied")]


def _record_size(record: Mapping[str, Any]) -> int:
    """Measure a record as the response serializes it, not in compact form."""
    return len(json.dumps(record, default=str))


def _bound_criterion_commands(
    records: list[dict[str, Any]],
    budget: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Fit the criterion verdicts into ``budget`` by shedding observed evidence only.

    Every criterion keeps its own record, so a satisfied entry stays authoritative
    and no required command silently disappears. Only the diagnostic
    ``observed_forms`` list shrinks, oldest form first, and each record counts what
    it dropped in ``omitted_observed_form_count``.
    """
    bounded: list[dict[str, Any]] = []
    for record in records:
        copied = dict(record)
        forms = copied.get("observed_forms")
        if isinstance(forms, list):
            copied["observed_forms"] = list(forms)
        bounded.append(copied)
    sizes = [_record_size(record) for record in bounded]
    shrinkable = {
        index
        for index, record in enumerate(bounded)
        if isinstance(record.get("observed_forms"), list) and record["observed_forms"]
    }
    total = sum(sizes)
    omitted = 0
    while total > budget and shrinkable:
        index = max(shrinkable, key=lambda key: (sizes[key], key))
        record = bounded[index]
        forms = record["observed_forms"]
        forms.pop(0)
        record["omitted_observed_form_count"] = (
            int(record.get("omitted_observed_form_count") or 0) + 1
        )
        omitted += 1
        if not forms:
            del record["observed_forms"]
            shrinkable.discard(index)
        size = _record_size(record)
        total += size - sizes[index]
        sizes[index] = size
    return bounded, omitted, max(0, budget - total)


def bound_review_details(details: dict[str, Any], criteria: str) -> dict[str, Any]:
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
    remaining = REVIEW_COMMAND_BUDGET
    criterion_records = details.get("criterion_commands")
    if isinstance(criterion_records, list):
        details["criterion_commands"], omitted_forms, remaining = _bound_criterion_commands(
            criterion_records, remaining
        )
        if omitted_forms:
            details["omitted_criterion_observed_form_count"] = omitted_forms
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
