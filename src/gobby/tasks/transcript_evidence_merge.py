"""Combine session evidence while preserving each session's transcript order."""

from dataclasses import replace
from datetime import datetime

from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)


def merge_transcript_evidence(*evidence_sets: TranscriptEvidence) -> TranscriptEvidence:
    """Merge session evidence into one transcript position sequence.

    Each session numbers ``order`` from its own counter, so raw values are not
    comparable across the owner and closing sessions the close path merges.
    Sessions interleave by provider timestamp without reordering within a
    session, and the merged edits and runs are renumbered as one sequence so
    every consumer can compare positions.
    """
    streams: list[list[tuple[datetime, TranscriptValidationRun | TranscriptEdit]]] = []
    author_run_ids = {
        id(run) for evidence in evidence_sets for run in evidence.off_task_author_runs
    }
    for evidence in evidence_sets:
        items: list[tuple[datetime, TranscriptValidationRun | TranscriptEdit]] = [
            (run.completed_at, run)
            for run in (
                *evidence.validation_runs,
                *evidence.command_runs,
                *evidence.off_task_author_runs,
            )
        ]
        items.extend((edit.timestamp, edit) for edit in evidence.edits)
        items.sort(key=lambda item: item[1].order)
        if items:
            streams.append(items)

    cursors = [0] * len(streams)
    runs: list[TranscriptValidationRun] = []
    author_runs: list[TranscriptValidationRun] = []
    edits: list[TranscriptEdit] = []
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
        if isinstance(item, TranscriptValidationRun):
            target = author_runs if id(item) in author_run_ids else runs
            target.append(replace(item, order=position))
        else:
            edits.append(replace(item, order=position))

    return TranscriptEvidence(
        validation_runs=tuple(run for run in runs if run.categories),
        command_runs=tuple(run for run in runs if not run.categories),
        off_task_author_runs=tuple(author_runs),
        excluded_runs=tuple(run for evidence in evidence_sets for run in evidence.excluded_runs),
        edits=tuple(edits),
        task_claims=tuple(claim for evidence in evidence_sets for claim in evidence.task_claims),
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
