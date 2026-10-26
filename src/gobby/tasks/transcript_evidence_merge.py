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
    boundary = min(
        (run.completed_at for run in (*live.validation_runs, *live.command_runs)),
        default=None,
    )
    if boundary is None:
        return archive
    return replace(
        archive,
        validation_runs=tuple(
            run for run in archive.validation_runs if run.completed_at < boundary
        ),
        command_runs=tuple(run for run in archive.command_runs if run.completed_at < boundary),
    )


def merge_transcript_evidence(*evidence_sets: TranscriptEvidence) -> TranscriptEvidence:
    """Merge session evidence into one transcript position sequence.

    Each transcript numbers ``order`` from its own counter. Interleave by
    provider timestamp without reordering within a transcript, then renumber
    the runs so consumers can compare their positions.
    """
    streams: list[list[tuple[datetime, TranscriptValidationRun]]] = []
    for evidence in evidence_sets:
        items: list[tuple[datetime, TranscriptValidationRun]] = [
            (run.completed_at, run)
            for run in (
                *evidence.validation_runs,
                *evidence.command_runs,
            )
        ]
        items.sort(key=lambda item: item[1].order)
        if items:
            streams.append(items)

    cursors = [0] * len(streams)
    runs: list[TranscriptValidationRun] = []
    position = 0
    while True:
        heads = [
            (streams[index][cursor][0], index)
            for index, cursor in enumerate(cursors)
            if cursor < len(streams[index])
        ]
        if not heads:
            break
        _stamp, index = min(heads)
        item = streams[index][cursors[index]][1]
        cursors[index] += 1
        position += 1
        runs.append(replace(item, order=position))

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
