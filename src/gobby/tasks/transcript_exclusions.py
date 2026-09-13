"""Read pre-link observations without changing the credited transcript snapshot."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from gobby.config.validation_detection import ValidationDetectionConfig
from gobby.sessions.machine_scope import require_local_session_ownership
from gobby.storage.session_models import Session
from gobby.tasks.transcript_evidence import (
    TranscriptValidationRun,
    _coerce_datetime,
    _derive_transcript_evidence_sync,
)
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
    """
    start = _coerce_datetime(window_start)
    if start is None:
        return ()
    evidence, _snapshot = await run_in_transcript_evidence_pool(
        _derive_transcript_evidence_sync,
        session,
        None,
        detection,
        set(),
        repo_path,
        archive_dir,
        require_local_session_ownership(session),
        None,
    )
    return tuple(
        replace(run, output=None)
        for run in (*evidence.validation_runs, *evidence.command_runs)
        if run.started_at < start
    )
