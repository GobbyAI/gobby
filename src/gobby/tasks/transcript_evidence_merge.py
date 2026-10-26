"""Combine session evidence while preserving each session's transcript order."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptValidationRun


def merge_path_evidence(
    primary: TranscriptEvidence,
    supplemental: Sequence[tuple[str, TranscriptEvidence]],
) -> TranscriptEvidence:
    """Merge a session's primary transcript with its supplemental transcripts by path."""
    return merge_transcript_evidence(
        primary,
        *(
            archive_before_live(evidence, primary) if path.endswith(".gz") else evidence
            for path, evidence in supplemental
        ),
    )


def archive_before_live(
    archive: TranscriptEvidence, live: TranscriptEvidence
) -> TranscriptEvidence:
    """Retain only an archive's prefix missing from the resumed live transcript."""
    return archive_before(archive, earliest_completion(live))


def earliest_completion(evidence: TranscriptEvidence) -> datetime | None:
    """The first completion a transcript's runs record, or None when it recorded none."""
    return min(
        (run.completed_at for run in (*evidence.validation_runs, *evidence.command_runs)),
        default=None,
    )


def archive_before(archive: TranscriptEvidence, boundary: datetime | None) -> TranscriptEvidence:
    """Retain the archive runs that finished before ``boundary``."""
    if boundary is None:
        return archive
    return replace(
        archive,
        validation_runs=tuple(
            run for run in archive.validation_runs if run.completed_at < boundary
        ),
        command_runs=tuple(run for run in archive.command_runs if run.completed_at < boundary),
    )


def interleave_streams(
    streams: Sequence[Sequence[TranscriptValidationRun]],
) -> list[tuple[int, TranscriptValidationRun]]:
    """Interleave ordered transcript streams by provider time, ties to the earlier stream."""
    cursors = [0] * len(streams)
    merged: list[tuple[int, TranscriptValidationRun]] = []
    while True:
        heads = [
            (streams[index][cursor].completed_at, index)
            for index, cursor in enumerate(cursors)
            if cursor < len(streams[index])
        ]
        if not heads:
            return merged
        _stamp, index = min(heads)
        merged.append((index, streams[index][cursors[index]]))
        cursors[index] += 1


def merge_transcript_evidence(*evidence_sets: TranscriptEvidence) -> TranscriptEvidence:
    """Merge session evidence into one transcript position sequence.

    Each transcript numbers ``order`` from its own counter. Interleave by
    provider timestamp without reordering within a transcript, then renumber
    the runs so consumers can compare their positions.
    """
    streams: list[list[TranscriptValidationRun]] = []
    for evidence in evidence_sets:
        items = sorted(
            (*evidence.validation_runs, *evidence.command_runs), key=lambda run: run.order
        )
        if items:
            streams.append(items)
    runs = [
        replace(run, order=position)
        for position, (_, run) in enumerate(interleave_streams(streams), start=1)
    ]

    return TranscriptEvidence(
        validation_runs=tuple(run for run in runs if run.categories),
        command_runs=tuple(run for run in runs if not run.categories),
        attempted_paths=tuple(
            dict.fromkeys(path for evidence in evidence_sets for path in evidence.attempted_paths)
        ),
        sessions=tuple(
            dict.fromkeys(session for evidence in evidence_sets for session in evidence.sessions)
        ),
        degraded_capabilities=tuple(
            dict.fromkeys(
                item for evidence in evidence_sets for item in evidence.degraded_capabilities
            )
        ),
        latest_record_at=max(
            (
                evidence.latest_record_at
                for evidence in evidence_sets
                if evidence.latest_record_at is not None
            ),
            default=None,
        ),
    )
