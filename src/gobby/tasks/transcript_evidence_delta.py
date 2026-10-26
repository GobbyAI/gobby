"""The runs a stream snapshot keeps, and what a resumed stop sends back for them.

A stream stop resumes from a lean snapshot: the runs a later record can still rewrite,
plus the last run, which the next record may settle. Every other run is final, so the
stream already holds its last state and nothing later changes it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptPathDelta,
    TranscriptValidationRun,
)


def lean_runs(runs: Sequence[TranscriptValidationRun]) -> tuple[TranscriptValidationRun, ...]:
    """The rewritable runs in order, then the last run."""
    if not runs:
        return ()
    rewritable = tuple(run for run in runs[:-1] if _rewritable(run))
    return (*rewritable, runs[-1])


def _rewritable(run: TranscriptValidationRun) -> bool:
    """Background receipts, rtk recall replacements and recovered outputs rewrite earlier runs."""
    return (
        run.background_job_id is not None
        or run.background_output_path is not None
        or run.background_monitor_id is not None
        or run.output_recovered_from is not None
        or "rtk recall" in (run.output or "")
    )


def path_delta(
    path: str,
    evidence: TranscriptEvidence,
    previous: Sequence[TranscriptValidationRun] | None,
) -> TranscriptPathDelta:
    """Split a derivation against the runs the caller's snapshot held for this path.

    ``previous`` None means the path parsed cold, so its whole window is the delta.
    Otherwise the delta is the runs created since and the held runs a later record rewrote.
    """
    if previous is None:
        return TranscriptPathDelta(path=path, evidence=evidence, resumed=False)
    held = {run.run_id: run for run in previous}
    current = (*evidence.validation_runs, *evidence.command_runs)
    patches = tuple(run for run in current if run.run_id in held and held[run.run_id] != run)
    appended = replace(
        evidence,
        validation_runs=tuple(run for run in evidence.validation_runs if run.run_id not in held),
        command_runs=tuple(run for run in evidence.command_runs if run.run_id not in held),
    )
    return TranscriptPathDelta(path=path, evidence=appended, resumed=True, patches=patches)
