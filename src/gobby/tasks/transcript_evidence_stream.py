"""Session validation evidence accumulated from worker deltas.

The found-work stop gate evaluates on every stop. The stream holds each path's runs
by creation id and applies the worker's deltas in place, so a stop costs its new runs
rather than the session's history. A single transcript answers with the ledger's
reduced runs, which the gate decides exactly as it would the full window. Several
transcripts merge their full windows, because the merge renumbers across paths.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from gobby.tasks.transcript_evidence_merge import merge_path_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptEvidenceDelta,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_validation_ledger import TranscriptValidationLedger

_Change = tuple[TranscriptValidationRun | None, TranscriptValidationRun]


class TranscriptEvidenceStream:
    """One session's accumulated evidence, tied to the worker checkpoint it reflects."""

    def __init__(self) -> None:
        self.checkpoint: tuple[object, ...] | None = None
        self._runs: dict[str, dict[int, TranscriptValidationRun]] = {}
        self._meta: dict[str, TranscriptEvidence] = {}
        self._ledger: TranscriptValidationLedger | None = None

    def reset(self) -> None:
        self.checkpoint = None
        self._runs = {}
        self._meta = {}
        self._ledger = None

    def apply(
        self, delta: TranscriptEvidenceDelta, project_path: str | None = None
    ) -> TranscriptEvidence | None:
        """Fold ``delta`` in and return the session's evidence, or None without a base to extend."""
        # A resumed delta extends only the checkpoint it was derived from. A concurrent
        # derivation may have moved the stream on since, so refuse and let the caller
        # derive cold rather than append runs the stream already holds.
        resumed = any(item.resumed for item in delta.paths)
        if resumed and not self._extends(delta):
            return None
        changes: list[_Change] = []
        runs_by_path: dict[str, dict[int, TranscriptValidationRun]] = {}
        for item in delta.paths:
            if item.resumed:
                held = self._runs[item.path]
                for patch in item.patches:
                    changes.append((held[patch.run_id], patch))
                    held[patch.run_id] = patch
                for run in _runs_of(item.evidence):
                    changes.append((None, run))
                    held[run.run_id] = run
            else:
                held = {run.run_id: run for run in _runs_of(item.evidence)}
            runs_by_path[item.path] = held
        self.checkpoint = delta.checkpoint
        self._runs = runs_by_path
        self._meta = {
            item.path: replace(item.evidence, validation_runs=(), command_runs=())
            for item in delta.paths
        }
        primary = delta.paths[0].path
        if len(delta.paths) > 1:
            self._ledger = None
            return merge_path_evidence(
                _evidence_of(self._meta[primary], runs_by_path[primary].values()),
                [
                    (
                        item.path,
                        _evidence_of(self._meta[item.path], runs_by_path[item.path].values()),
                    )
                    for item in delta.paths[1:]
                ],
            )
        ledger = self._ledger
        if (
            not resumed
            or ledger is None
            or ledger.project_path != project_path
            or not ledger.apply(changes)
        ):
            ledger = TranscriptValidationLedger.of(project_path, runs_by_path[primary].values())
        self._ledger = ledger
        return replace(self._meta[primary], validation_runs=ledger.reduced(), command_runs=())

    def _extends(self, delta: TranscriptEvidenceDelta) -> bool:
        """Whether the held runs are exactly the state a resumed delta was derived from."""
        if delta.base != self.checkpoint or set(self._runs) != {item.path for item in delta.paths}:
            return False
        for item in delta.paths:
            if not item.resumed:
                continue
            held = self._runs[item.path]
            if any(patch.run_id not in held for patch in item.patches):
                return False
            if any(run.run_id in held for run in _runs_of(item.evidence)):
                return False
        return True


def _runs_of(evidence: TranscriptEvidence) -> tuple[TranscriptValidationRun, ...]:
    return (*evidence.validation_runs, *evidence.command_runs)


def _evidence_of(
    meta: TranscriptEvidence, runs: Iterable[TranscriptValidationRun]
) -> TranscriptEvidence:
    held = tuple(runs)
    return replace(
        meta,
        validation_runs=tuple(run for run in held if run.categories),
        command_runs=tuple(run for run in held if not run.categories),
    )
