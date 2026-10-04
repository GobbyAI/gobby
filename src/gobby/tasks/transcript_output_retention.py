"""Which transcript command output derivation keeps, and for how long."""

from __future__ import annotations

import re
from dataclasses import replace

from gobby.tasks.transcript_evidence_models import (
    TranscriptValidationRun,
    TranscriptValidationSegment,
)

# The general exit-preserving normalizer strips the `rtk` executable itself.
_RTK_RECALL_RE = re.compile(r"(?:uv run )?rtk recall ([0-9a-f]{12,64})")


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
    if segments or _RTK_RECALL_RE.fullmatch(command.strip()):
        return output, output_truncated
    return None, False


def _drop_settled_command_output(
    runs: list[TranscriptValidationRun],
) -> list[TranscriptValidationRun]:
    """Keep a non-validation run's output only while it is the latest run.

    Its output is read only then: by the Codex wrapper dedupe and as an rtk
    recall receipt. Validation output stays for the gates. ``_retained_output``
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
        else run
        for index, run in enumerate(runs)
    ]
