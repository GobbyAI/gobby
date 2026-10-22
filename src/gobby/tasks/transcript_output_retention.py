"""Which transcript command output derivation keeps, and for how long."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from gobby.tasks.transcript_evidence_models import (
    TranscriptValidationRun,
    TranscriptValidationSegment,
)
from gobby.tasks.transcript_outcomes import extract_outcome

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
    runs[index] = replace(
        original,
        output=receipt.output,
        output_truncated=False,
        output_recovered_from=receipt.command,
        output_recovered_at=receipt.completed_at,
    )


def _retained_output(
    command: str,
    segments: tuple[TranscriptValidationSegment, ...],
    output: str | None,
    output_truncated: bool,
) -> tuple[str | None, bool]:
    """Keep output for validation runs and recall receipts.

    Non-validation shell output is never read after outcome extraction, and every
    retained byte is unpickled from the derivation pool while holding the GIL.
    """
    if segments or _RTK_RECALL_RE.fullmatch(command.strip()):
        return output, output_truncated
    return None, False


def _drop_settled_command_output(
    runs: list[TranscriptValidationRun],
) -> list[TranscriptValidationRun]:
    """Keep a non-validation run's output only while it is the latest run.

    Validation output stays for found-work checks. ``_retained_output``
    drops non-validation output as runs are recorded; this also sheds settled
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
        else run
        for index, run in enumerate(runs)
    ]
