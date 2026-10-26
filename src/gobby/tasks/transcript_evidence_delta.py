"""The runs a stream snapshot keeps, and what a resumed stop sends back for them.

A stop resumes from a lean snapshot: background jobs still awaiting a receipt, the newest
settled runs a later record can rewrite, and the last run. Other runs are final for the
snapshot. The stream keeps the open failures and greens the gate still reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptPathDelta,
    TranscriptValidationRun,
)

#: Settled rewritable runs a snapshot keeps. Older settled runs are final: a receipt that
#: rewrites one of them is not reconciled on resume.
_SETTLED_WINDOW = 16


def lean_runs(runs: Sequence[TranscriptValidationRun]) -> tuple[TranscriptValidationRun, ...]:
    """Background jobs awaiting a receipt, the newest settled rewritable runs, then the last run.

    A pending job keeps its place at any age, because its receipt can arrive at any stop.
    The settled window is fixed, so a long session of finished background jobs keeps the
    same snapshot size.
    """
    if not runs:
        return ()
    head = runs[:-1]
    rewritable = [index for index, run in enumerate(head) if _rewritable(run)]
    settled = [index for index in rewritable if not _pending(head[index])]
    kept = {index for index in rewritable if _pending(head[index])}
    kept.update(settled[-_SETTLED_WINDOW:])
    return (*(head[index] for index in sorted(kept)), runs[-1])


def _pending(run: TranscriptValidationRun) -> bool:
    """A background job whose completion receipt has not arrived yet."""
    return run.exit_code is None and (
        run.background_job_id is not None or run.background_output_path is not None
    )


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
