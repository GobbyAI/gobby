"""Session validation evidence accumulated from worker deltas.

The found-work stop gate evaluates on every stop. Deriving the whole window each
time makes the pool transfer grow with session length, so the gate keeps the runs
it has already seen and asks the worker only for runs appended since its
checkpoint. The merged result matches a full derivation of the same window.
"""

from __future__ import annotations

from dataclasses import replace

from gobby.tasks.transcript_evidence_merge import merge_path_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptEvidenceDelta,
    TranscriptValidationRun,
)


class TranscriptEvidenceStream:
    """One session's accumulated evidence, tied to the worker checkpoint it reflects."""

    def __init__(self) -> None:
        self.checkpoint: tuple[object, ...] | None = None
        self._paths: dict[str, TranscriptEvidence] = {}

    def reset(self) -> None:
        self.checkpoint = None
        self._paths = {}

    def apply(self, delta: TranscriptEvidenceDelta) -> TranscriptEvidence | None:
        """Fold ``delta`` in and return the merged evidence, or None without a base to extend."""
        # A resumed delta extends only the checkpoint it was derived from. A concurrent
        # derivation may have moved the stream on since, so refuse and let the caller
        # derive cold rather than append runs the stream already holds.
        if delta.base != self.checkpoint and any(item.resumed for item in delta.paths):
            return None
        paths: dict[str, TranscriptEvidence] = {}
        for item in delta.paths:
            prior: tuple[TranscriptValidationRun, ...] = ()
            if item.resumed:
                held = self._paths.get(item.path)
                if held is None:
                    return None
                prior = (*held.validation_runs, *held.command_runs)
            runs = (*prior, *item.evidence.validation_runs, *item.evidence.command_runs)
            paths[item.path] = replace(
                item.evidence,
                validation_runs=tuple(run for run in runs if run.categories),
                command_runs=tuple(run for run in runs if not run.categories),
            )
        self._paths = paths
        self.checkpoint = delta.checkpoint
        primary, *supplemental = delta.paths
        return merge_path_evidence(
            paths[primary.path],
            [(item.path, paths[item.path]) for item in supplemental],
        )
