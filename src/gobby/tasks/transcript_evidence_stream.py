"""Session validation evidence accumulated from worker deltas.

The found-work stop gate evaluates on every stop. The stream holds only what a later stop
can still read: each path's lean runs and the ledger's open failures and green window.
A stop applies the worker's delta to that held set and judges it again, so its cost follows
the new runs and the fixed windows rather than the session's history. Several transcripts
interleave their held runs in provider time, the order a full merge uses, and an archive
contributes only the prefix its live transcript does not cover.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from gobby.tasks.transcript_evidence_delta import lean_runs
from gobby.tasks.transcript_evidence_merge import interleave_streams, merge_transcript_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptEvidenceDelta,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_validation_ledger import TranscriptValidationLedger


class TranscriptEvidenceStream:
    """One session's held evidence, tied to the worker checkpoint it reflects."""

    def __init__(self) -> None:
        self.checkpoint: tuple[object, ...] | None = None
        self._runs: dict[str, dict[int, TranscriptValidationRun]] = {}
        self._meta: dict[str, TranscriptEvidence] = {}
        #: The earliest completion the primary transcript has recorded. An archive's runs
        #: at or after it duplicate the live transcript and are left out of the merge.
        self._boundary: datetime | None = None

    def reset(self) -> None:
        self.checkpoint = None
        self._runs = {}
        self._meta = {}
        self._boundary = None

    def apply(
        self, delta: TranscriptEvidenceDelta, project_path: str | None = None
    ) -> TranscriptEvidence | None:
        """Fold ``delta`` in and return the session's evidence, or None without a base to extend."""
        # A resumed delta extends only the checkpoint it was derived from. A concurrent
        # derivation may have moved the stream on since, so refuse and let the caller
        # derive cold rather than append runs the stream already holds.
        if any(item.resumed for item in delta.paths) and not self._extends(delta):
            return None
        held: dict[str, dict[int, TranscriptValidationRun]] = {}
        for item in delta.paths:
            runs = dict(self._runs[item.path]) if item.resumed else {}
            for patch in item.patches:
                runs[patch.run_id] = patch
            for run in _runs_of(item.evidence):
                runs[run.run_id] = run
            held[item.path] = runs
        primary = delta.paths[0]
        self._boundary = _earliest(
            self._boundary if primary.resumed else None,
            *(run.completed_at for run in _runs_of(primary.evidence)),
        )
        streams = [
            sorted(
                (
                    run
                    for run in held[item.path].values()
                    if _visible(item.path, run, self._boundary)
                ),
                key=lambda run: run.order,
            )
            for item in delta.paths
        ]
        lean = {id(run) for stream in streams for run in lean_runs(stream)}
        sequence = interleave_streams(streams)
        judged = [replace(run, order=position) for position, (_, run) in enumerate(sequence, 1)]
        reduced = {id(run) for run in TranscriptValidationLedger.of(project_path, judged).reduced()}
        kept: dict[str, dict[int, TranscriptValidationRun]] = {
            item.path: {} for item in delta.paths
        }
        for (index, run), judge in zip(sequence, judged, strict=True):
            if id(run) in lean or id(judge) in reduced:
                kept[delta.paths[index].path][run.run_id] = run
        self._runs = kept
        self.checkpoint = delta.checkpoint
        self._meta = {
            item.path: replace(item.evidence, validation_runs=(), command_runs=())
            for item in delta.paths
        }
        return merge_transcript_evidence(
            *(
                _evidence_of(
                    self._meta[item.path],
                    sorted(kept[item.path].values(), key=lambda run: run.order),
                )
                for item in delta.paths
            )
        )

    def _extends(self, delta: TranscriptEvidenceDelta) -> bool:
        """Whether the held runs are exactly the state a resumed delta was derived from."""
        if delta.base != self.checkpoint or set(self._runs) != {item.path for item in delta.paths}:
            return False
        return not any(
            run.run_id in self._runs[item.path]
            for item in delta.paths
            if item.resumed
            for run in _runs_of(item.evidence)
        )


def _runs_of(evidence: TranscriptEvidence) -> tuple[TranscriptValidationRun, ...]:
    return (*evidence.validation_runs, *evidence.command_runs)


def _evidence_of(
    meta: TranscriptEvidence, runs: list[TranscriptValidationRun]
) -> TranscriptEvidence:
    return replace(
        meta,
        validation_runs=tuple(run for run in runs if run.categories),
        command_runs=tuple(run for run in runs if not run.categories),
    )


def _visible(path: str, run: TranscriptValidationRun, boundary: datetime | None) -> bool:
    """Whether a run belongs to the merged view: archive runs before the live boundary only."""
    return not (path.endswith(".gz") and boundary is not None and run.completed_at >= boundary)


def _earliest(*stamps: datetime | None) -> datetime | None:
    return min((stamp for stamp in stamps if stamp is not None), default=None)
