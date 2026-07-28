"""Transcript and Git-state support for read-only close evaluation."""

from __future__ import annotations

import logging
from typing import Any

from gobby.config.validation_detection import resolve_validation_detection_config
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.tasks.transcript_evidence import (
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
    derive_transcript_evidence,
    merge_transcript_evidence,
)
from gobby.workflows.task_dirty_state import committable_task_paths, has_committable_edits

__all__ = [
    "committable_task_paths",
    "has_committable_edits",
]

logger = logging.getLogger(__name__)


async def derive_close_transcript_evidence(
    ctx: RegistryContext,
    *,
    task_id: str,
    owner_session_id: str,
    closing_session_id: str,
    owner_window_start: str | None,
    task_edited_files: set[str],
    repo_path: str,
) -> TranscriptEvidence:
    """Parse and merge owning and closing session transcripts."""
    detection = resolve_validation_detection_config(
        daemon_config=ctx.config,
        project_path=repo_path,
    )
    archive_dir = (
        getattr(getattr(ctx.config, "sessions", None), "transcript_archive_dir", None)
        if ctx.config
        else None
    )
    evidence: list[TranscriptEvidence] = []
    for session_id in dict.fromkeys((owner_session_id, closing_session_id)):
        session = ctx.session_manager.get(session_id)
        if session is None:
            raise TranscriptEvidenceUnavailable(
                f"Session {session_id} was not found",
                source="unknown",
                attempted_paths=(),
            )
        window_start = (
            owner_window_start
            if session_id == owner_session_id
            else task_session_window_start(ctx, task_id, session_id) or session.created_at
        )
        evidence.append(
            await derive_transcript_evidence(
                session,
                window_start,
                detection,
                task_edited_files,
                repo_path,
                archive_dir=archive_dir,
            )
        )
    return merge_transcript_evidence(*evidence)


def claimed_session_window_start(
    ctx: RegistryContext,
    task: Any,
    resolved_id: str,
) -> str | None:
    """Return the latest claim window for the owner."""
    owner_session_id = get_claimed_session_id(task)
    return (
        task_session_window_start(ctx, resolved_id, owner_session_id, claimed_only=True)
        if owner_session_id
        else None
    )


def task_session_window_start(
    ctx: RegistryContext,
    task_id: str,
    session_id: str,
    *,
    claimed_only: bool = False,
) -> str | None:
    """Resolve the earliest admissible transcript event for a task-session link."""
    try:
        rows = ctx.session_task_manager.get_task_sessions(task_id)
    except Exception as exc:
        logger.debug("Failed to load task-session window: %s", exc)
        return None
    allowed = {"claimed"} if claimed_only else {"claimed", "worked_on", "created"}
    for row in rows:
        if (
            str(row.get("session_id")) == session_id
            and (row.get("action") or row.get("session_action")) in allowed
        ):
            return _format_git_since(row.get("created_at") or row.get("link_created_at"))
    return None


def _format_git_since(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    return str(value).strip() or None
