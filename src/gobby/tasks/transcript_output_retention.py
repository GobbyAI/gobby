"""Which transcript command output derivation keeps, and for how long."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from gobby.tasks.command_equivalence import pytest_collection_target
from gobby.tasks.transcript_background import deliver_output
from gobby.tasks.transcript_evidence_models import (
    OutputDelivery,
    TranscriptValidationRun,
    TranscriptValidationSegment,
)
from gobby.tasks.transcript_git_sources import CLEAN_HEAD_COMMAND
from gobby.tasks.transcript_outcomes import classify_validation_command_equivalence, extract_outcome

# The general exit-preserving normalizer strips the `rtk` executable itself.
_RTK_RECALL_RE = re.compile(r"(?:uv run )?rtk recall ([0-9a-f]{12,64})(?: --full)?")


def recover_rtk_output(runs: list[TranscriptValidationRun], result: Any) -> None:
    """Attach a complete native recall receipt to its unique failed test run."""
    receipt = runs[-1]
    match = _RTK_RECALL_RE.fullmatch(receipt.command.strip())
    if match is None or receipt.wrapped:
        return
    outcome, exit_code, unknown_reason = extract_outcome(result)
    if (
        outcome != "success"
        or not receipt.output
        or receipt.output_truncated
        # A persisted preview must leave the original authenticated pointer intact.
        or "<persisted-output>" in receipt.output
    ):
        return
    runs[-1] = replace(receipt, outcome=outcome, exit_code=exit_code, unknown_reason=unknown_reason)
    reference = re.compile(rf"(?m)^[ \t]*\[full output: rtk recall {match.group(1)}\][ \t]*\r?$")
    originals = [
        index
        for index, run in enumerate(runs[:-1])
        if run.outcome == "failure"
        and "test" in run.categories
        and (
            reference.search(run.output or "")
            or re.fullmatch(
                rf"(?:uv run )?rtk recall {match.group(1)}(?: --full)?",
                run.output_recovered_from or "",
            )
        )
    ]
    if len(originals) != 1:
        return
    index = originals[0]
    original = runs[index]
    if original.output_deliveries:
        runs[index] = deliver_output(
            original,
            OutputDelivery(
                receipt.completed_at, receipt.command, receipt.output, False, appends=False
            ),
        )
        return
    runs[index] = replace(
        original,
        output=receipt.output,
        output_truncated=False,
        output_recovered_from=receipt.command,
        output_recovered_at=receipt.completed_at,
    )


def _collection_receipt(command: str) -> bool:
    """Gate 10 reads collection output even though collection is not test execution."""
    core = classify_validation_command_equivalence(command).core_command or command
    return pytest_collection_target(core) is not None


def _retained_output(
    command: str,
    segments: tuple[TranscriptValidationSegment, ...],
    output: str | None,
    output_truncated: bool,
) -> tuple[str | None, bool]:
    """Keep output only where a gate reads it: validation runs and recall receipts.

    Review-only shell output is never read after outcome extraction, and every
    retained byte is unpickled from the derivation pool while holding the GIL.
    """
    if (
        segments
        or _collection_receipt(command)
        or _RTK_RECALL_RE.fullmatch(command.strip())
        or command.strip() == CLEAN_HEAD_COMMAND
    ):
        return output, output_truncated
    return None, False


def _drop_settled_command_output(
    runs: list[TranscriptValidationRun],
) -> list[TranscriptValidationRun]:
    """Keep a non-validation run's output only while it is the latest run.

    Clean HEAD receipts also survive incremental resumes until Git restore proof.
    Validation output stays for the gates. ``_retained_output``
    drops review-only output as runs are recorded; this also sheds settled
    recall receipts and the shell output that snapshots written before it still
    carry, on their next resume rather than through a full re-parse.
    """
    last = len(runs) - 1
    return [
        replace(run, output=None)
        if index < last
        and run.output is not None
        and not run.categories
        and not run.validation_segments
        and run.command.strip() != CLEAN_HEAD_COMMAND
        and not _collection_receipt(run.command)
        else run
        for index, run in enumerate(runs)
    ]
