"""Ownership-aware task transition helpers.

This module centralizes task lifecycle writes that previously lived across MCP
wrappers and ad hoc storage updates. Phase 1 keeps the legacy `status` field as
the outward lifecycle surface, but makes session ownership explicit via
`claimed_by_session_id`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from gobby.plans.bootstrap_ledger import bootstrap_ledger_path_for_task, verify_bootstrap_ledger
from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._crud import _session_exists, get_task, update_task
from gobby.storage.tasks._lifecycle_events import (
    TaskLifecycleEventManager,
    record_lifecycle_event,
)
from gobby.storage.tasks._models import (
    UNSET,
    MaybeUnset,
    Task,
    TaskAlreadyClaimedError,
    TaskClosedError,
)
from gobby.storage.tasks._stage_states import (
    NoCurrentStageError,
    StageStatesManager,
    _close_task_in_txn,
)
from gobby.tasks.state_semantics import is_task_closed, normalize_de_escalation_target_status

logger = logging.getLogger(__name__)

VALID_LIFECYCLES = frozenset(
    {
        "open",
        "plan_review",
        "test_arch",
        "expanding",
        "in_development",
        "holistic_review",
        "pr",
        "merging",
        "merged",
    }
)
VALID_STATUSES = frozenset(
    {"open", "in_progress", "needs_review", "review_approved", "closed", "escalated"}
)
POST_BUILD_TRANSITIONS = frozenset(
    {
        ("plan_review", "open", "approved"),
        ("plan_review", "open", "rejected"),
        ("test_arch", "open", "approved"),
        ("test_arch", "open", "rejected"),
        ("expanding", "open", "success"),
        ("expanding", "open", "failure"),
        ("in_development", "open", "dev_complete"),
        ("in_development", "needs_review", "qa_approved"),
        ("in_development", "needs_review", "qa_rejected"),
        ("in_development", "open", "all_leaves_parked"),
        ("holistic_review", "open", "approved"),
        ("holistic_review", "open", "rejected"),
        ("pr", "open", "pr_opened"),
        ("pr", "escalated", "pr_opened"),
        ("pr", "needs_review", "approved"),
        ("merging", "open", "merged"),
        ("merging", "open", "merge_failed"),
    }
)
POST_BUILD_DESTINATIONS = frozenset(
    {
        ("test_arch", "open"),
        ("plan_review", "open"),
        ("expanding", "open"),
        ("in_development", "open"),
        ("in_development", "needs_review"),
        ("holistic_review", "review_approved"),
        ("holistic_review", "open"),
        ("pr", "open"),
        ("pr", "needs_review"),
        ("merging", "open"),
        ("merged", "closed"),
        ("merging", "escalated"),
    }
)


def _state(lifecycle: str | None, status: str | None) -> str:
    return f"{lifecycle or 'open'}:{status or 'open'}"


def _stage_states(db: DatabaseProtocol) -> StageStatesManager:
    return StageStatesManager(db, TaskLifecycleEventManager(db))


def _artifact_columns(db: DatabaseProtocol) -> set[str]:
    return {row["name"] for row in db.fetchall("PRAGMA table_info(task_artifacts)")}


def _ensure_artifact_row(db: DatabaseProtocol, task_id: str) -> None:
    db.execute(
        """
        INSERT INTO task_artifacts (task_id, updated_at)
        VALUES (?, datetime('now'))
        ON CONFLICT(task_id) DO UPDATE SET updated_at = task_artifacts.updated_at
        """,
        (task_id,),
    )


def _apply_artifact_updates(
    db: DatabaseProtocol,
    task_id: str,
    updates: dict[str, str | int | None],
) -> None:
    if not updates:
        return
    available = _artifact_columns(db)
    filtered = {key: value for key, value in updates.items() if key in available}
    if not filtered:
        return
    _ensure_artifact_row(db, task_id)
    assignments = [f"{column} = ?" for column in filtered]
    params = [*filtered.values(), task_id]
    db.execute(
        f"""
        UPDATE task_artifacts
        SET {", ".join(assignments)}, updated_at = datetime('now')
        WHERE task_id = ?
        """,  # nosec B608 - columns are filtered against table metadata.
        tuple(params),
    )


def _increment_artifact_counter(db: DatabaseProtocol, task_id: str, column: str) -> int:
    if column not in _artifact_columns(db):
        return 0
    _ensure_artifact_row(db, task_id)
    db.execute(
        f"""
        UPDATE task_artifacts
        SET {column} = COALESCE({column}, 0) + 1,
            updated_at = datetime('now')
        WHERE task_id = ?
        """,  # nosec B608 - column is caller-owned static artifact metadata.
        (task_id,),
    )
    row = db.fetchone(f"SELECT {column} FROM task_artifacts WHERE task_id = ?", (task_id,))
    return int(row[column] or 0) if row is not None else 0


def _cascade_merged_close(db: DatabaseProtocol, task_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    rows = db.fetchall(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM tasks WHERE parent_task_id = ?
            UNION ALL
            SELECT tasks.id FROM tasks JOIN subtree ON tasks.parent_task_id = subtree.id
        )
        SELECT id FROM subtree
        """,
        (task_id,),
    )
    for row in rows:
        update_task(
            db,
            row["id"],
            lifecycle="merged",
            status="closed",
            assignee=None,
            claimed_by_session_id=None,
            closed_at=now,
            closed_reason="merged",
        )
        record_lifecycle_event(
            db,
            row["id"],
            None,
            _state("merged", "closed"),
            "cascade_merged",
            by_actor="advance_lifecycle",
        )


def _reset_cited_subtasks(db: DatabaseProtocol, cited_subtasks: list[str] | None) -> None:
    for subtask_id in cited_subtasks or []:
        advance_lifecycle(
            db,
            subtask_id,
            "in_development",
            "open",
            side_effects={"reason": "holistic_rejection_cited_subtask", "clear_claim": True},
        )


def _all_leaves_parked_or_terminal(db: DatabaseProtocol, task_id: str) -> bool:
    rows = db.fetchall(
        """
        WITH RECURSIVE subtree(id, task_type, lifecycle, status, closed_at, escalated_at) AS (
            SELECT id, task_type, lifecycle, status, closed_at, escalated_at
            FROM tasks
            WHERE parent_task_id = ?
            UNION ALL
            SELECT tasks.id, tasks.task_type, tasks.lifecycle, tasks.status,
                   tasks.closed_at, tasks.escalated_at
            FROM tasks
            JOIN subtree ON tasks.parent_task_id = subtree.id
        )
        SELECT lifecycle, status, closed_at, escalated_at
        FROM subtree
        WHERE task_type != 'epic'
        """,
        (task_id,),
    )
    if not rows:
        return False
    for row in rows:
        parked = row["lifecycle"] == "holistic_review" and row["status"] == "review_approved"
        terminal = bool(row["closed_at"] or row["escalated_at"])
        merged = row["lifecycle"] == "merged" and row["status"] == "closed"
        if not (parked or terminal or merged):
            return False
    return True


def advance_lifecycle(
    db: DatabaseProtocol,
    task_id: str,
    to_lifecycle: str,
    to_status: str,
    side_effects: dict[str, Any] | None = None,
) -> Task:
    """Advance a task lifecycle tuple, apply side effects, and audit the transition."""
    if to_lifecycle not in VALID_LIFECYCLES:
        raise ValueError(f"Invalid lifecycle '{to_lifecycle}'")
    if to_status not in VALID_STATUSES:
        raise ValueError(f"Invalid task status '{to_status}'")

    task = get_task(db, task_id)
    effects = side_effects or {}
    from_lifecycle = (
        task.lifecycle.value if hasattr(task.lifecycle, "value") else str(task.lifecycle)
    )
    from_status = task.status
    if (
        (from_lifecycle, from_status, to_lifecycle, to_status)
        == ("in_development", "open", "holistic_review", "open")
        and task.task_type == "epic"
        and not _all_leaves_parked_or_terminal(db, task_id)
    ):
        raise ValueError(
            "Epic cannot enter holistic_review until all leaves are parked or terminal."
        )

    artifact_updates = dict(effects.get("artifact_updates") or {})
    for column in effects.get("clear_artifacts") or ():
        artifact_updates[column] = None
    for column in effects.get("clear_counters") or ():
        artifact_updates[column] = 0
    for column in effects.get("increment_counters") or ():
        _increment_artifact_counter(db, task_id, column)
    _apply_artifact_updates(db, task_id, artifact_updates)

    now = datetime.now(UTC).isoformat()
    update_task(
        db,
        task_id,
        lifecycle=to_lifecycle,
        status=to_status,
        assignee=None if effects.get("clear_claim", True) else UNSET,
        claimed_by_session_id=None if effects.get("clear_claim", True) else UNSET,
        escalated_at=None if effects.get("clear_escalation") else UNSET,
        escalation_reason=(
            None if effects.get("clear_escalation") else effects.get("escalation_reason", UNSET)
        ),
        closed_at=now if to_status == "closed" else UNSET,
        closed_reason=effects.get("closed_reason", UNSET),
    )
    if effects.get("cascade_close"):
        _cascade_merged_close(db, task_id)
    if effects.get("cited_subtasks"):
        _reset_cited_subtasks(db, effects.get("cited_subtasks"))

    record_lifecycle_event(
        db,
        task_id,
        _state(from_lifecycle, from_status),
        _state(to_lifecycle, to_status),
        str(effects.get("reason") or "advance_lifecycle"),
        by_actor=str(effects.get("by_actor") or "advance_lifecycle"),
    )
    return get_task(db, task_id)


def project_claim_status(current_status: str) -> str:
    """Project the legacy status used when a session claims a task."""
    return "in_progress" if current_status == "open" else current_status


def get_effective_claim_owner(task: Task, db: DatabaseProtocol) -> str | None:
    """Return the canonical owning session for a task during the migration."""
    if task.claimed_by_session_id:
        return task.claimed_by_session_id
    if task.assignee and _session_exists(db, task.assignee):
        return task.assignee
    return None


def claim_task(
    db: DatabaseProtocol,
    task_id: str,
    session_id: str,
    *,
    force: bool = False,
) -> Task:
    """Claim a task for a session, preserving non-open lifecycle states."""
    task = get_task(db, task_id)
    current_owner = get_effective_claim_owner(task, db)

    if is_task_closed(task):
        raise TaskClosedError(f"Cannot claim task {task_id}: task is closed")
    if current_owner and current_owner != session_id and not force:
        raise TaskAlreadyClaimedError(task_id, current_owner)

    status = project_claim_status(task.status)
    update_task(
        db,
        task_id,
        status=status if status != task.status else UNSET,
        assignee=session_id,
        claimed_by_session_id=session_id,
    )
    return get_task(db, task_id)


def release_task_claim(
    db: DatabaseProtocol,
    task_id: str,
    *,
    status: MaybeUnset[str | None] = UNSET,
    description: MaybeUnset[str | None] = UNSET,
    validation_fail_count: MaybeUnset[int | None] = UNSET,
    dispatch_failure_count: MaybeUnset[int | None] = UNSET,
    escalated_at: MaybeUnset[str | None] = UNSET,
    escalation_reason: MaybeUnset[str | None] = UNSET,
    validation_override_reason: MaybeUnset[str | None] = UNSET,
    labels: MaybeUnset[list[str] | None] = UNSET,
) -> Task:
    """Clear canonical and legacy ownership while optionally changing lifecycle state."""
    update_task(
        db,
        task_id,
        status=status,
        description=description,
        assignee=None,
        claimed_by_session_id=None,
        validation_fail_count=validation_fail_count,
        dispatch_failure_count=dispatch_failure_count,
        escalated_at=escalated_at,
        escalation_reason=escalation_reason,
        validation_override_reason=validation_override_reason,
        labels=labels,
    )
    return get_task(db, task_id)


def reopen_task(
    db: DatabaseProtocol,
    task_id: str,
    *,
    reason: str | None = None,
) -> Task:
    """Reopen a task to open status and clear ownership/closure metadata."""
    task = get_task(db, task_id)
    if task.status == "open":
        raise ValueError(f"Task {task_id} is already open")

    description = task.description
    if reason:
        reopen_note = f"\n\n[Reopened: {reason}]"
        description = (description or "") + reopen_note

    update_task(
        db,
        task_id,
        status="open",
        description=description if reason else UNSET,
        assignee=None,
        claimed_by_session_id=None,
        lifecycle_stage=None,
        closed_reason=None,
        closed_at=None,
        closed_in_session_id=None,
        closed_commit_sha=None,
        escalated_at=None,
        escalation_reason=None,
        validation_fail_count=0,
        dispatch_failure_count=0,
    )
    return get_task(db, task_id)


def escalate_task(
    db: DatabaseProtocol,
    task_id: str,
    *,
    reason: str,
    validation_override_reason: str | None = None,
) -> Task:
    """Escalate a task and release canonical ownership.

    When ``validation_override_reason`` is provided, it is persisted in the
    same write as the escalation so callers don't need a second update_task
    call that could fail after the escalation has already landed.
    """
    task = get_task(db, task_id)
    if task.is_escalated or is_task_closed(task):
        raise ValueError(f"Cannot escalate task with status '{task.status}'.")

    return release_task_claim(
        db,
        task_id,
        status="escalated",
        escalated_at=datetime.now(UTC).isoformat(),
        escalation_reason=reason,
        validation_override_reason=(
            validation_override_reason if validation_override_reason is not None else UNSET
        ),
    )


def de_escalate_task(
    db: DatabaseProtocol,
    task_id: str,
    *,
    reason: str,
    target_status: str | None = None,
    target_lifecycle: str | None = None,
    reset_validation: bool = False,
) -> Task:
    """Return an escalated task to an explicit next status."""
    task = get_task(db, task_id)
    if not task.is_escalated:
        raise ValueError(f"Task {task_id} is not escalated (current status: {task.status})")

    normalized_target = normalize_de_escalation_target_status(target_status)
    description = (
        f"{task.description}\n\nDe-escalated: {reason}"
        if task.description
        else f"De-escalated: {reason}"
    )

    update_task(
        db,
        task_id,
        lifecycle=target_lifecycle if target_lifecycle is not None else UNSET,
        status=normalized_target,
        description=description,
        escalated_at=None,
        escalation_reason=None,
        validation_fail_count=0 if reset_validation else UNSET,
    )
    return get_task(db, task_id)


def mark_task_needs_review(
    db: DatabaseProtocol,
    task_id: str,
    *,
    review_notes: str | None = None,
    by_session_id: str | None = None,
) -> Task:
    """Submit the current stage for review and release ownership."""
    task = get_task(db, task_id)
    stages = _stage_states(db)
    current = stages.current_stage(task_id)
    if current is None:
        raise NoCurrentStageError(task_id)
    stages.submit_for_review(
        task_id,
        current.stage_name,
        by_session_id=by_session_id,
        notes=review_notes,
    )
    description: MaybeUnset[str | None] = UNSET
    if review_notes:
        description = (task.description or "") + f"\n\n[Review Notes]\n{review_notes}"
    labels = [
        label for label in (task.labels or []) if label != "planning-current-verdict:rejected"
    ]

    update_task(
        db,
        task_id,
        description=description,
        labels=labels,
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)


def mark_task_review_approved(
    db: DatabaseProtocol,
    task_id: str,
    *,
    approval_notes: str | None = None,
    by_session_id: str | None = None,
) -> Task:
    """Approve review on the current stage and release ownership."""
    task = get_task(db, task_id)
    stages = _stage_states(db)
    current = stages.current_stage(task_id)
    if current is None:
        raise NoCurrentStageError(task_id)
    stages.approve_review(
        task_id,
        current.stage_name,
        by_session_id=by_session_id,
        notes=approval_notes,
    )
    description: MaybeUnset[str | None] = UNSET
    if approval_notes:
        description = (task.description or "") + f"\n\n[Approval Notes]\n{approval_notes}"

    update_task(
        db,
        task_id,
        description=description,
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)


def mark_task_review_rejected(
    db: DatabaseProtocol,
    task_id: str,
    *,
    rejection_notes: str | None = None,
    round_number: int | None = None,
    plan_hash: str | None = None,
    cited_subtasks: list[str] | None = None,
    by_session_id: str | None = None,
) -> Task:
    """Reject review on the current stage and release ownership."""
    task = get_task(db, task_id)
    normalized_round = None
    if round_number is not None:
        # Tools/routes may pass an int-like value; normalize once before validation.
        normalized_round = int(round_number)
        if normalized_round < 1:
            raise ValueError("round must be >= 1 when provided")

    stages = _stage_states(db)
    current = stages.current_stage(task_id)
    if current is None:
        raise NoCurrentStageError(task_id)
    notes = rejection_notes
    if plan_hash:
        notes = f"{notes or ''}\n\nplan_hash: {plan_hash}".strip()
    if cited_subtasks:
        notes = f"{notes or ''}\n\ncited_subtasks: {', '.join(cited_subtasks)}".strip()
    stages.reject_review(
        task_id,
        current.stage_name,
        reason=rejection_notes or "review_rejected",
        by_session_id=by_session_id,
        notes=notes,
    )

    description: MaybeUnset[str | None] = UNSET
    if rejection_notes:
        heading = (
            f"## Adversary Findings — Round {normalized_round}"
            if normalized_round is not None
            else "## Review Rejection"
        )
        section = f"{heading}\n\n{rejection_notes}"
        existing = task.description or ""
        # Re-running the same round must replace the prior section, not stack.
        # Mirrors the planning-round:N label dedup below — same idempotency policy.
        # Only attempt the in-place replacement for round-scoped headings; the
        # generic "## Review Rejection" heading is used for one-off rejections
        # without a round number and is allowed to stack.
        if normalized_round is not None and heading in existing:
            import re

            pattern = re.compile(
                rf"^{re.escape(heading)}.*?(?=^## Adversary Findings — Round |\Z)",
                re.DOTALL | re.MULTILINE,
            )
            description = pattern.sub(section.rstrip() + "\n\n", existing).rstrip() or section
        else:
            description = f"{existing}\n\n{section}" if existing else section

    labels = list(task.labels or [])
    if normalized_round is not None:
        labels = [label for label in labels if not label.startswith("planning-round:")]
        labels.append(f"planning-round:{normalized_round}")

    update_task(
        db,
        task_id,
        description=description,
        labels=labels if normalized_round is not None else UNSET,
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)


def mark_task_pr_opened(db: DatabaseProtocol, task_id: str, pr_url: str) -> Task:
    """Move PR-stage work into external review and persist the opened PR URL."""
    task = get_task(db, task_id)
    lifecycle = task.lifecycle.value if hasattr(task.lifecycle, "value") else str(task.lifecycle)
    if lifecycle != "pr" or task.status not in ("open", "escalated"):
        raise ValueError("PR can only be opened from (pr, open) or (pr, escalated)")
    return advance_lifecycle(
        db,
        task_id,
        "pr",
        "needs_review",
        {
            "reason": "pr_opened",
            "artifact_updates": {"pr_url": pr_url},
            "clear_escalation": True,
        },
    )


def mark_task_merged(
    db: DatabaseProtocol,
    task_id: str,
    *,
    pr_url: str | None = None,
    merge_sha: str | None = None,
) -> Task:
    """Mark a task subtree as merged and terminal."""
    return advance_lifecycle(
        db,
        task_id,
        "merged",
        "closed",
        {
            "reason": "merged",
            "artifact_updates": {"pr_url": pr_url, "merge_commit_sha": merge_sha},
            "cascade_close": True,
            "closed_reason": "merged",
        },
    )


def mark_task_merge_failed(
    db: DatabaseProtocol,
    task_id: str,
    reason: str,
    *,
    attended: bool = False,
) -> Task:
    """Record merge failure retry or attended escalation."""
    if attended:
        return advance_lifecycle(
            db,
            task_id,
            "merging",
            "escalated",
            {
                "reason": "merge_failed:max_attempts",
                "escalation_reason": f"merge_failed:{reason}",
                "increment_counters": ("merge_attempts",),
                "clear_claim": True,
            },
        )
    return advance_lifecycle(
        db,
        task_id,
        "merging",
        "open",
        {
            "reason": f"merge_failed:{reason}",
            "increment_counters": ("merge_attempts",),
        },
    )


def close_task(
    db: DatabaseProtocol,
    task_id: str,
    *,
    reason: str | None = None,
    force: bool = False,
    closed_in_session_id: str | None = None,
    closed_commit_sha: str | None = None,
    validation_override_reason: str | None = None,
) -> Task:
    """Close a task and clear active ownership metadata."""
    with db.transaction() as conn:
        if bootstrap_ledger_path_for_task(db, task_id) is not None:
            verify_bootstrap_ledger(db, task_id)
        _close_task_in_txn(
            conn,
            task_id,
            reason=reason,
            commit_sha=closed_commit_sha,
            closed_in_session_id=closed_in_session_id,
            force=force,
            validation_override_reason=validation_override_reason,
        )
    return get_task(db, task_id)


def reconcile_task_state(
    db: DatabaseProtocol,
    task_id: str,
    *,
    status: str,
    title: MaybeUnset[str | None] = UNSET,
    description: MaybeUnset[str | None] = UNSET,
    priority: MaybeUnset[int | None] = UNSET,
) -> Task:
    """Apply externally-sourced lifecycle state without reopening generic update paths.

    This helper is intentionally narrow: it exists for sync/reconciliation flows
    that need to project external lifecycle state into a task without exposing
    raw status/ownership mutation through LocalTaskManager.update_task().
    """
    update_task(
        db,
        task_id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        assignee=None,
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)
