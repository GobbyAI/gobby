"""Read pre-link observations without changing the credited transcript snapshot."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from gobby.config.validation_detection import ValidationDetectionConfig
from gobby.sessions.machine_scope import require_local_session_ownership
from gobby.storage.session_models import Session
from gobby.tasks.transcript_evidence import (
    _coerce_datetime,
    _derive_transcript_evidence_sync,
    _EvidenceSnapshot,
    _load_snapshot,
    _store_snapshot,
)
from gobby.tasks.transcript_evidence_models import TranscriptValidationRun
from gobby.tasks.transcript_evidence_pool import run_in_transcript_evidence_pool


async def derive_prelink_runs(
    session: Session,
    window_start: datetime | str | None,
    detection: ValidationDetectionConfig,
    repo_path: str,
    *,
    archive_dir: str | None = None,
) -> tuple[TranscriptValidationRun, ...]:
    """Use the existing parser, but never read/write its credited-window cache.

    Keep only pre-link commands, without outputs or edits. This second read is
    diagnostic-only: it cannot supply validation, TDD, or review-fingerprint credit.
    It resumes from its own snapshot, keyed apart from the credited window, so a
    repeated close evaluation parses only the transcript suffix appended since
    the previous pass instead of the whole file.
    """
    start = _coerce_datetime(window_start)
    if start is None:
        return ()
    snapshot_key = f"{session.id}:prelink"
    runs, snapshot = await run_in_transcript_evidence_pool(
        _derive_prelink_runs_sync,
        start,
        session,
        detection,
        repo_path,
        archive_dir,
        require_local_session_ownership(session),
        _load_snapshot(snapshot_key),
    )
    if snapshot is not None:
        _store_snapshot(snapshot_key, snapshot)
    return runs


def _derive_prelink_runs_sync(
    start: datetime,
    session: Session,
    detection: ValidationDetectionConfig,
    repo_path: str,
    archive_dir: str | None,
    local_machine_id: str,
    snapshot: _EvidenceSnapshot | None,
) -> tuple[tuple[TranscriptValidationRun, ...], _EvidenceSnapshot | None]:
    # Each rebuilt run re-classifies its command, so stripping stays in the pool
    # with the parse; on the daemon loop it stalled close previews (#22708).
    evidence, snapshot = _derive_transcript_evidence_sync(
        session, None, detection, set(), repo_path, archive_dir, local_machine_id, snapshot
    )
    runs = tuple(
        replace(run, output=None)
        for run in (*evidence.validation_runs, *evidence.command_runs)
        if run.started_at < start
    )
    return runs, snapshot
