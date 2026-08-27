"""Transcript and Git-state support for read-only close evaluation."""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from gobby.config.validation_detection import resolve_validation_detection_config
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.tasks import Task
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.tasks.transcript_evidence import (
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
    derive_transcript_evidence,
    merge_transcript_evidence,
)
from gobby.workflows.task_dirty_state import committable_task_paths, has_committable_edits

__all__ = [
    "CloseAttributionSnapshot",
    "CloseEvaluationFingerprint",
    "closes_as_structural_parent",
    "committable_task_paths",
    "fingerprint_differences",
    "has_committable_edits",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloseAttributionSnapshot:
    """Mutable session-attribution inputs consumed by the close checklist."""

    owner_session_id: str
    attributed: bool
    raw_paths: frozenset[str]
    edited_paths: frozenset[str]
    had_attributed_edits: bool
    claim_started_at: str | None


@dataclass(frozen=True)
class CloseEvaluationFingerprint:
    """Gate-relevant mutable state captured before the bounded review."""

    closed_at: datetime | None
    is_escalated: bool
    validation_criteria: str | None
    category: str | None
    task_type: str
    claimed_by_session_id: str | None
    parent_task_id: str | None
    children_state: tuple[tuple[str, str | None, bool], ...]
    attribution: CloseAttributionSnapshot | None

    @classmethod
    def capture(
        cls,
        task: Task,
        *,
        children_state: tuple[tuple[str, str | None, bool], ...],
        attribution: CloseAttributionSnapshot | None,
    ) -> CloseEvaluationFingerprint:
        """Build a stable fingerprint from values actually used by the gates."""
        return cls(
            closed_at=task.closed_at,
            is_escalated=task.is_escalated,
            validation_criteria=task.validation_criteria,
            category=task.category,
            task_type=task.task_type,
            claimed_by_session_id=get_claimed_session_id(task),
            parent_task_id=task.parent_task_id,
            children_state=children_state,
            attribution=attribution,
        )


def closes_as_structural_parent(task: Task, *, has_children: bool) -> bool:
    """Whether a close skips the leaf gates because the task only organizes work.

    A task with children is organizational only when it owns no work of its
    own. A claimed task, or one with linked commits, is a worked leaf that
    gained found-work children; its own gates still apply (#20969 closed
    without them once #21046 hung under it). The evaluation and the commit
    recheck must agree on this, or the recheck captures a different
    attribution shape and every close of such a task reports stale state
    (#21093).
    """
    owns_work = task.claimed_by_session_id is not None or bool(task.commits)
    return task.task_type == "epic" or (has_children and not owns_work)


def fingerprint_differences(
    expected: CloseEvaluationFingerprint | None,
    fresh: CloseEvaluationFingerprint,
) -> list[str]:
    """Name the fingerprint fields that changed, nested into the attribution."""
    if expected is None:
        return ["evaluation"]
    changed: list[str] = []
    for field_info in fields(CloseEvaluationFingerprint):
        name = field_info.name
        before = getattr(expected, name)
        after = getattr(fresh, name)
        if before == after:
            continue
        if name == "attribution" and before is not None and after is not None:
            changed.extend(
                f"attribution.{inner.name}"
                for inner in fields(CloseAttributionSnapshot)
                if getattr(before, inner.name) != getattr(after, inner.name)
            )
            continue
        changed.append(name)
    return changed


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
    """Parse and merge every session transcript that worked the task.

    The owner and closing sessions are required. Every other session that
    claimed or worked the task (an implementer that handed off to a QA
    session, for example) contributes within its own link window, so a
    red/green cycle recorded before the handoff still satisfies the TDD gate.
    History never blocks a close: a linked session that no longer exists or
    has no readable transcript is skipped.
    """
    config = ctx.config
    detection = resolve_validation_detection_config(
        daemon_config=config,
        project_path=repo_path,
    )
    archive_dir = config.session_lifecycle.transcript_archive_dir if config is not None else None
    windows: dict[str, str | None] = {owner_session_id: owner_window_start}
    if closing_session_id not in windows:
        windows[closing_session_id] = task_session_window_start(ctx, task_id, closing_session_id)
    required = frozenset(windows)
    for session_id, window_start in _linked_session_windows(ctx, task_id).items():
        windows.setdefault(session_id, window_start)
    evidence: list[TranscriptEvidence] = []
    for session_id, window_start in windows.items():
        session = ctx.session_manager.get(session_id)
        if session is None:
            if session_id in required:
                raise TranscriptEvidenceUnavailable(
                    f"Session {session_id} was not found",
                    source="unknown",
                    attempted_paths=(),
                )
            logger.debug("Skipping close evidence for missing linked session %s", session_id)
            continue
        effective_window: str | datetime | None = window_start
        if session_id != owner_session_id:
            effective_window = window_start or session.created_at
        try:
            evidence.append(
                await derive_transcript_evidence(
                    session,
                    effective_window,
                    detection,
                    task_edited_files,
                    repo_path,
                    archive_dir=archive_dir,
                )
            )
        except TranscriptEvidenceUnavailable as exc:
            if session_id in required:
                raise
            logger.warning("Skipping close evidence for linked session %s: %s", session_id, exc)
    return merge_transcript_evidence(*evidence)


_EVIDENCE_LINK_ACTIONS = frozenset({"claimed", "worked_on"})


def _linked_session_windows(ctx: RegistryContext, task_id: str) -> dict[str, str | None]:
    """Map each session that claimed or worked the task to its earliest such link."""
    try:
        rows = ctx.session_task_manager.get_task_sessions(task_id)
    except Exception as exc:
        logger.debug("Failed to load task-session history: %s", exc)
        return {}
    windows: dict[str, str | None] = {}
    # Rows arrive newest first, so the last assignment leaves the earliest link.
    for row in rows:
        if (row.get("action") or row.get("session_action")) not in _EVIDENCE_LINK_ACTIONS:
            continue
        windows[str(row.get("session_id"))] = _format_git_since(
            row.get("created_at") or row.get("link_created_at")
        )
    return windows


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
