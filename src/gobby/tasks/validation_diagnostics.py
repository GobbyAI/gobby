"""Diagnostic-only explanations for validation observations excluded from credit."""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from typing import Any

from gobby.tasks.criterion_commands import edit_details, execution_details, first_invalidating_edit
from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptValidationRun,
)


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
