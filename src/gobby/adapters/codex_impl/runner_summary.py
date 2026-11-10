"""Outcome of a literal test runner whose cell prints a result but no structured outcome."""

from __future__ import annotations

from typing import Any

from gobby.adapters.codex_impl.pending_execution import PendingExecution
from gobby.config.validation_detection import classify_validation_segments
from gobby.config.validation_outcomes import runner_reported_failures, runner_reported_success


def runner_summary_result(execution: PendingExecution, output: str) -> dict[str, Any] | None:
    if not execution.literal_command:
        return None
    matches = classify_validation_segments(execution.literal_command)
    if len(matches) != 1 or matches[0].is_compound or "test" not in matches[0].categories:
        return None
    if runner_reported_failures(output):
        success = False
    elif runner_reported_success(output):
        success = True
    else:
        return None
    return {
        "success": success,
        "output": output,
        "outcome_provenance": "codex.execution_chain.runner_output",
    }
