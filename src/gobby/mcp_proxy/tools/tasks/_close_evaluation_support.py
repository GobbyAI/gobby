"""Transcript and Git-state support for read-only close evaluation."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.code_index.storage import CodeIndexStorage
from gobby.config.validation_detection import (
    ValidationDetectionConfig,
    resolve_validation_detection_config,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.mcp_proxy.tools.tasks._task_scope import collect_commit_paths_async
from gobby.storage.session_models import Session
from gobby.storage.tasks import Task, TaskNotFoundError
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.tasks.transcript_evidence import (
    derive_transcript_evidence,
    merge_transcript_evidence,
)
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
    TranscriptTaskClaim,
)
from gobby.tasks.transcript_exclusions import derive_prelink_runs
from gobby.tasks.transcript_sync import transcript_sync_point
from gobby.workflows.task_dirty_state import (
    committable_task_paths,
    committable_task_paths_async,
    has_committable_edits,
)

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
    used_commit_fallback: bool = False


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
    task_checkout_paths: frozenset[tuple[str, str]],
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
            task_checkout_paths=task_checkout_paths,
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
    owner_used_commit_fallback: bool = False,
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
    from gobby.workflows.task_claim_state import (
        other_task_edited_checkout_paths,
        task_edited_checkout_history_paths,
        task_edited_checkout_paths,
    )

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
        variables = ctx.session_var_manager.get_variables(session_id)
        # Released pairs still prove the earlier worker's transcript edits after
        # a transfer. Live ownership checks continue to use only the live ledger.
        task_checkout_paths = task_edited_checkout_paths(
            variables, task_id
        ) | task_edited_checkout_history_paths(variables, task_id)
        paths_by_root: dict[str, set[str]] = {}
        for root, path in task_checkout_paths:
            paths_by_root.setdefault(root, set()).add(path)
        committable_pairs: set[tuple[str, str]] = set()
        for root, paths in paths_by_root.items():
            committable_pairs.update(
                (root, path) for path in await committable_task_paths_async(paths, root)
            )
        # Scratch edits must not invalidate the earlier worker's clean runs.
        task_checkout_paths = frozenset(committable_pairs)
        task_links: list[dict[str, Any]] | None = None
        window_end: float | None = None
        if session_id not in required:
            task_links = await asyncio.to_thread(
                ctx.session_task_manager.get_session_tasks, session_id
            )
            window_end = _moved_on_epoch(task_links, task_id, effective_window)
            if not task_checkout_paths and not variables.get(
                "task_edited_file_checkouts_history_started_at"
            ):
                task_checkout_paths, effective_window = await _legacy_task_checkout_proof(
                    ctx,
                    session,
                    task_id,
                    effective_window,
                    window_end,
                    repo_path,
                )
        if (
            session_id == owner_session_id
            and owner_used_commit_fallback
            and not any(path in task_edited_files for _, path in task_checkout_paths)
        ):
            # Only the owner's commit-recovery attribution proves these task files
            # in the closing checkout. Ignored-only pairs cannot anchor those files.
            # A linked session with an empty ledger may have edited
            # the same path later for a different task.
            root = os.path.realpath(repo_path)
            task_checkout_paths = frozenset((root, path) for path in task_edited_files)
        if task_checkout_paths:
            if task_links is None:
                task_links = await asyncio.to_thread(
                    ctx.session_task_manager.get_session_tasks, session_id
                )
            completed_other_tasks = _closed_before_window_task_ids(
                task_links, task_id, effective_window
            )
            other_task_paths = other_task_edited_checkout_paths(
                variables, task_id, completed_other_tasks
            )
            # A live or overlapping historical pair cannot identify which
            # transcript edit belongs to this close.
            task_checkout_paths -= other_task_paths
            legacy_closed_tasks = _legacy_closed_other_tasks(
                task_links,
                task_id,
                effective_window,
                variables.get("task_edited_file_checkouts_history_started_at"),
            )
            if task_checkout_paths and legacy_closed_tasks:
                # A close before path history began dropped that task's ledger, so
                # its commits are the proof of which paths it owned.
                task_checkout_paths = await _without_closed_task_paths(
                    task_checkout_paths, legacy_closed_tasks, repo_path
                )
        session_task_files = task_edited_files
        if session_id != owner_session_id:
            # A linked session's own proven pairs attribute its edits; a reclaiming
            # owner's ledger can hold only what it touched afterwards (#23017).
            session_task_files = task_edited_files | {path for _, path in task_checkout_paths}
        try:
            session_evidence = await _derive_session_evidence_at_sync_point(
                session,
                effective_window,
                detection,
                session_task_files,
                repo_path,
                task_checkout_paths,
                archive_dir=archive_dir,
            )
            if session_id not in required or session_id == owner_session_id:
                # The owner leaves too: once this task is handed off it may claim newer
                # work, whose runs must neither fail nor pass this close (#23665).
                if task_links is None:
                    task_links = await asyncio.to_thread(
                        ctx.session_task_manager.get_session_tasks, session_id
                    )
                # session_tasks keeps one row per task, so a return to this task
                # after a departure shows only in the transcript's own claims.
                claims = await _resolve_transcript_claims(
                    ctx, session, task_links, task_id, session_evidence.task_claims
                )
                presence = _task_presence(task_links, task_id, effective_window, claims)
                if presence is not None:
                    session_evidence = _while_on_task(session_evidence, presence)
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


def _evidence_epoch(value: str | datetime | None) -> float | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
    return None


def _moved_on_epoch(
    task_links: Iterable[dict[str, Any]],
    task_id: str,
    window_start: str | datetime | None,
) -> float | None:
    """When a linked session first claimed a different task after joining this one.

    Its later shell runs and edits belong to that work, so a failing validation
    there must not count against this task's close (#22884 found work, #23017).
    """
    start = _evidence_epoch(window_start)
    if start is None:
        return None
    later_claims = [
        epoch
        for row in task_links
        if (row.get("action") or row.get("session_action")) == "claimed"
        and getattr(row.get("task"), "id", None) != task_id
        and (epoch := _evidence_epoch(row.get("link_created_at"))) is not None
        and epoch > start
    ]
    return min(later_claims, default=None)


async def _resolve_transcript_claims(
    ctx: RegistryContext,
    session: Session,
    task_links: Iterable[dict[str, Any]],
    task_id: str,
    claims: Iterable[TranscriptTaskClaim],
) -> list[tuple[str | None, datetime]]:
    """Resolve each transcript claim to a task UUID, or ``None`` when it cannot be."""
    known = {task_id} | {
        linked_id for row in task_links if (linked_id := getattr(row.get("task"), "id", None))
    }
    project_id = getattr(session, "project_id", None)

    def resolve(ref: str) -> str | None:
        if ref in known:
            return ref
        if not project_id:
            return None
        try:
            return resolve_task_id_for_mcp(ctx.task_manager, ref, project_id)
        except (TaskNotFoundError, ValueError):
            return None

    def resolve_all() -> list[tuple[str | None, datetime]]:
        return [(resolve(claim.task_ref), claim.claimed_at) for claim in claims]

    return await asyncio.to_thread(resolve_all)


def _task_presence(
    task_links: Iterable[dict[str, Any]],
    task_id: str,
    window_start: str | datetime | None,
    transcript_claims: Iterable[tuple[str | None, datetime]],
) -> list[tuple[float, bool]] | None:
    """When a linked session left this task for another and when it returned.

    Its runs and edits while away belong to that other work, so a failing
    validation there must not count against this task's close (#22884, #23017).
    Each event is ``(epoch, returned)``; ``None`` means it never left. An
    unresolvable transcript claim counts as leaving, so it never adds credit.
    """
    start = _evidence_epoch(window_start)
    if start is None:
        return None
    events = [
        (epoch, getattr(row.get("task"), "id", None) == task_id)
        for row in task_links
        if (row.get("action") or row.get("session_action")) == "claimed"
        and (epoch := _evidence_epoch(row.get("link_created_at"))) is not None
        and epoch > start
    ]
    events.extend(
        (epoch, claimed_id == task_id)
        for claimed_id, claimed_at in transcript_claims
        if (epoch := _evidence_epoch(claimed_at)) is not None and epoch > start
    )
    if all(returned for _, returned in events):
        return None
    return sorted(events)


def _while_on_task(
    evidence: TranscriptEvidence, presence: list[tuple[float, bool]]
) -> TranscriptEvidence:
    """Keep only the runs and edits a linked session made while working this task."""

    def on_task(value: datetime) -> bool:
        epoch = _evidence_epoch(value)
        if epoch is None:
            return False
        on = True
        for at, returned in presence:
            if at > epoch:
                break
            on = returned
        return on

    return replace(
        evidence,
        validation_runs=tuple(r for r in evidence.validation_runs if on_task(r.started_at)),
        command_runs=tuple(r for r in evidence.command_runs if on_task(r.started_at)),
        edits=tuple(e for e in evidence.edits if on_task(e.timestamp)),
    )


def _closed_before_window_task_ids(
    task_links: Iterable[dict[str, Any]],
    task_id: str,
    window_start: str | datetime | None,
) -> frozenset[str]:
    """Identify other tasks whose links and closure predate this evidence window."""
    window_epoch = _evidence_epoch(window_start)
    if window_epoch is None:
        return frozenset()
    completed: set[str] = set()
    ambiguous: set[str] = set()
    for row in task_links:
        if (row.get("action") or row.get("session_action")) not in _EVIDENCE_LINK_ACTIONS:
            continue
        task = row.get("task")
        other_id = getattr(task, "id", None)
        if not isinstance(other_id, str) or other_id == task_id:
            continue
        closed_epoch = _evidence_epoch(getattr(task, "closed_at", None))
        link_epoch = _evidence_epoch(row.get("link_created_at"))
        if (
            closed_epoch is not None
            and link_epoch is not None
            and link_epoch <= closed_epoch < window_epoch
        ):
            completed.add(other_id)
        else:
            ambiguous.add(other_id)
    return frozenset(completed - ambiguous)


def _legacy_closed_other_tasks(
    task_links: Iterable[dict[str, Any]],
    task_id: str,
    window_start: str | datetime | None,
    history_started_at: Any,
) -> tuple[Any, ...]:
    """Return closed other tasks whose edits may predate durable path history.

    Closing a task drops its live ledger, so the history ledger is the only record
    of its paths, and it holds nothing a task linked before history began edited
    first. An open task keeps its live ledger, which the caller already
    subtracts, so only these closes need other proof.
    """
    window_epoch = _evidence_epoch(window_start)
    history_epoch = (
        float(history_started_at)
        if isinstance(history_started_at, (int, float)) and not isinstance(history_started_at, bool)
        else None
    )
    closed: dict[str, Any] = {}
    for row in task_links:
        if (row.get("action") or row.get("session_action")) not in _EVIDENCE_LINK_ACTIONS:
            continue
        task = row.get("task")
        other_id = getattr(task, "id", None)
        if not isinstance(other_id, str) or other_id == task_id:
            continue
        closed_epoch = _evidence_epoch(getattr(task, "closed_at", None))
        if closed_epoch is None:
            continue
        if window_epoch is not None and closed_epoch < window_epoch:
            continue
        link_epoch = _evidence_epoch(row.get("link_created_at"))
        if history_epoch is None or link_epoch is None or link_epoch <= history_epoch:
            closed[other_id] = task
    return tuple(closed.values())


async def _legacy_task_checkout_proof(
    ctx: RegistryContext,
    session: Session,
    task_id: str,
    window_start: str | datetime | None,
    window_end: float | None,
    repo_path: str,
) -> tuple[frozenset[tuple[str, str]], str | datetime | None]:
    """Recover pre-ledger edits only in one original, registered task checkout.

    A task commit alone cannot attribute another session's edits in the closing
    checkout. The registered worktree adds the exact root and its creation time;
    the claim and moved-on window still bound that session's original work.
    """
    denied: tuple[frozenset[tuple[str, str]], str | datetime | None] = (frozenset(), window_start)
    start = _evidence_epoch(window_start)
    if start is None:
        return denied
    worktrees = await asyncio.to_thread(
        ctx.worktree_manager.list_worktrees, task_id=task_id, limit=None
    )
    if not isinstance(worktrees, list):
        return denied
    roots: dict[str, float] = {}
    for worktree in worktrees:
        if (
            worktree.task_id != task_id
            or worktree.project_id != session.project_id
            or worktree.machine_id != session.machine_id
            or not isinstance(worktree.worktree_path, str)
        ):
            return denied
        created = _evidence_epoch(worktree.created_at)
        if created is None or created < start or (window_end is not None and created >= window_end):
            continue
        root = os.path.realpath(worktree.worktree_path)
        roots[root] = max(roots.get(root, created), created)
    if len(roots) != 1:
        return denied
    root, created = next(iter(roots.items()))
    if not await asyncio.to_thread(_same_git_checkout, root, repo_path):
        return denied
    task = await asyncio.to_thread(ctx.task_manager.get_task, task_id)
    commits = getattr(task, "commits", None)
    if (
        not isinstance(commits, list)
        or not commits
        or not all(isinstance(sha, str) for sha in commits)
    ):
        return denied
    try:
        paths = await collect_commit_paths_async(commits, root)
    except RuntimeError:
        return denied
    paths = await committable_task_paths_async(paths, root)
    return frozenset((root, path) for path in paths), datetime.fromtimestamp(created, UTC)


def _same_git_checkout(root: str, repo_path: str) -> bool:
    """A registered root must still name a checkout of this repository."""
    common: list[str] = []
    for checkout in (root, repo_path):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=checkout,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        common.append(os.path.realpath(result.stdout.strip()))
    return common[0] == common[1]


async def _without_closed_task_paths(
    task_checkout_paths: frozenset[tuple[str, str]],
    closed_tasks: Iterable[Any],
    repo_path: str,
) -> frozenset[tuple[str, str]]:
    """Drop target paths that a closed task's own commits also touched."""
    commit_shas = [sha for task in closed_tasks for sha in (getattr(task, "commits", None) or ())]
    try:
        owned = await collect_commit_paths_async(commit_shas, repo_path)
    except RuntimeError as exc:
        # Without the closed task's paths no edit is provably the target's own.
        # Independent validation runs remain admissible with no credited edits.
        logger.warning("Closed-task path ownership is unavailable: %s", exc)
        return frozenset()
    return frozenset(pair for pair in task_checkout_paths if pair[1] not in owned)


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
