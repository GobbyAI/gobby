"""Transcript and Git-state support for read-only close evaluation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from typing import Any

from gobby.code_index.storage import CodeIndexStorage
from gobby.config.validation_detection import (
    ValidationDetectionConfig,
    resolve_validation_detection_config,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.session_models import Session
from gobby.storage.tasks import Task
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.tasks.transcript_evidence import (
    derive_transcript_evidence,
    merge_transcript_evidence,
)
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
)
from gobby.tasks.transcript_exclusions import derive_prelink_runs
from gobby.tasks.transcript_sync import transcript_sync_point
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
    clean_proof_paths: frozenset[str]
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


#: A provider that flushes its transcript at turn end can trail its own live activity
#: sidecar. Close waits this long for the transcript to catch up to the sync point
#: taken at call time before reporting the evidence unavailable.
_TRANSCRIPT_CATCHUP_TIMEOUT_SECONDS = 15.0
#: How often to re-derive while waiting for the flush.
_TRANSCRIPT_CATCHUP_INTERVAL_SECONDS = 1.0
#: Slack between a sidecar record and the transcript line describing the same activity.
_TRANSCRIPT_CATCHUP_TOLERANCE_SECONDS = 5.0


async def _derive_session_evidence_at_sync_point(
    session: Session,
    window_start: str | datetime | None,
    detection: ValidationDetectionConfig,
    task_edited_files: set[str],
    repo_path: str,
    *,
    archive_dir: str | None,
) -> TranscriptEvidence:
    """Derive one session's evidence once its transcript covers live provider activity.

    A headless Grok run is a single turn and writes ``updates.jsonl`` when that turn
    ends, so a close called mid-turn parses a transcript holding none of the commands
    the worker actually ran (#22367). Waiting bounded for the flush preserves that
    evidence, and a transcript still behind at the deadline is reported unavailable —
    a retryable infrastructure failure — rather than judged as though it were complete.
    """
    sync_point = await asyncio.to_thread(transcript_sync_point, session)
    tolerance = timedelta(seconds=_TRANSCRIPT_CATCHUP_TOLERANCE_SECONDS)
    deadline = time.monotonic() + _TRANSCRIPT_CATCHUP_TIMEOUT_SECONDS
    while True:
        evidence = await derive_transcript_evidence(
            session,
            window_start,
            detection,
            task_edited_files,
            repo_path,
            archive_dir=archive_dir,
        )
        if sync_point is None:
            return evidence
        latest = evidence.latest_record_at
        if latest is not None and latest >= sync_point - tolerance:
            return evidence
        if time.monotonic() >= deadline:
            raise TranscriptEvidenceUnavailable(
                f"Transcript for {session.source} session {session.ref} is still behind "
                f"live activity at {sync_point.isoformat()}: newest parsed record "
                f"{latest.isoformat() if latest is not None else 'none'}.",
                source=session.source,
                attempted_paths=tuple(evidence.attempted_paths),
            )
        await asyncio.sleep(_TRANSCRIPT_CATCHUP_INTERVAL_SECONDS)


async def derive_close_transcript_evidence(
    ctx: RegistryContext,
    *,
    task_id: str,
    owner_session_id: str,
    closing_session_id: str,
    owner_window_start: str | None,
    task_edited_files: set[str],
    repo_path: str,
    require_task_link: bool = False,
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
        if windows[session_id] is None:
            windows[session_id] = window_start
    if require_task_link:
        # Optional no-edit evidence must not credit a caller's unrelated session.
        # A known claim window or a claimed/worked_on link bounds admissible work.
        windows = {session_id: start for session_id, start in windows.items() if start is not None}
        required = required.intersection(windows)
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
            session_evidence = await _derive_session_evidence_at_sync_point(
                session,
                effective_window,
                detection,
                task_edited_files,
                repo_path,
                archive_dir=archive_dir,
            )
            try:
                excluded = await derive_prelink_runs(
                    session, effective_window, detection, repo_path, archive_dir=archive_dir
                )
            except TranscriptEvidenceUnavailable as exc:
                logger.warning(
                    "Pre-link diagnostics unavailable for session %s: %s", session_id, exc
                )
                excluded = ()
            evidence.append(
                replace(session_evidence, excluded_runs=excluded) if excluded else session_evidence
            )
        except TranscriptEvidenceUnavailable as exc:
            if session_id in required:
                raise
            logger.warning("Skipping close evidence for linked session %s: %s", session_id, exc)
    # Merging rebuilds every run, and each rebuild re-classifies its command, so a
    # long session's merge stays off the event loop (#22708).
    return await asyncio.to_thread(merge_transcript_evidence, *evidence)


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
        windows[str(row.get("session_id"))] = format_git_since(
            row.get("created_at") or row.get("link_created_at")
        )
    return windows


def claimed_session_window_start(
    ctx: RegistryContext,
    task: Any,
    resolved_id: str,
) -> str | None:
    """Return the owner's latest claim window, or the earliest linked window when unowned.

    Escalation (manual or automatic) clears the owner and de-escalation never
    restores it, so an owner-only rule would stop scanning task-tagged commits
    for the rest of the task's life (#21531). Without an owner the earliest
    ``claimed``/``worked_on`` link across the task's sessions bounds the scan the
    same way transcript evidence is bounded; sessions that never linked to the
    task still contribute nothing.
    """
    owner_session_id = get_claimed_session_id(task)
    if owner_session_id:
        return task_session_window_start(ctx, resolved_id, owner_session_id, claimed_only=True)
    try:
        rows = ctx.session_task_manager.get_task_sessions(resolved_id)
    except Exception as exc:
        logger.debug("Failed to load task-session window: %s", exc)
        return None
    earliest: Any = None
    # Rows arrive newest first, so the last evidence link is the earliest.
    for row in rows:
        if (row.get("action") or row.get("session_action")) in _EVIDENCE_LINK_ACTIONS:
            earliest = row.get("created_at") or row.get("link_created_at")
    return format_git_since(earliest)


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
            return format_git_since(row.get("created_at") or row.get("link_created_at"))
    return None


def task_edit_languages(
    ctx: RegistryContext, project_id: str, edits: Iterable[TranscriptEdit]
) -> dict[str, str]:
    """Return gcode's indexed language for each edited path the code index knows.

    Close freshness lets an unknown path invalidate every earlier run, so a failed
    lookup degrades to an empty mapping and the conservative any-edit rule.
    """
    paths = sorted({edit.path for edit in edits})
    if not paths:
        return {}
    storage = CodeIndexStorage(ctx.task_manager.db)
    try:
        rows = [(path, storage.get_file(project_id, path)) for path in paths]
    except Exception as exc:
        logger.debug("Edit language lookup failed for project %s: %s", project_id, exc)
        return {}
    return {path: row.language for path, row in rows if row is not None}


def format_git_since(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    return str(value).strip() or None
