"""Raw task update storage primitives."""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import table_column_names
from gobby.storage.tasks._models import (
    UNSET,
    Isolation,
    MaybeUnset,
    validate_category,
    validate_implementation_domain,
    validate_task_type,
)
from gobby.storage.tasks._read import get_task
from gobby.utils.datetime import utc_now


def _locked_parent_task_id(conn: Any, task_id: str) -> str | None:
    row = conn.execute(
        "SELECT parent_task_id FROM tasks WHERE id = %s FOR UPDATE",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Task {task_id} not found")
    return cast(str | None, row["parent_task_id"])


def _validate_parent_task_id_update(
    conn: Any, task_id: str, proposed_parent_task_id: str | None
) -> None:
    _locked_parent_task_id(conn, task_id)
    if proposed_parent_task_id is None:
        return
    if proposed_parent_task_id == task_id:
        raise ValueError("Cannot set a task as its own parent")

    ancestor_id: str | None = proposed_parent_task_id
    visited: set[str] = set()
    while ancestor_id:
        if ancestor_id == task_id:
            raise ValueError("Cannot set a task parent to one of its descendants")
        if ancestor_id in visited:
            raise ValueError("Cannot set task parent because the parent hierarchy has a cycle")
        visited.add(ancestor_id)
        ancestor_id = _locked_parent_task_id(conn, ancestor_id)


def update_task(
    db: HubDatabase,
    task_id: str,
    title: MaybeUnset[str | None] = UNSET,
    description: MaybeUnset[str | None] = UNSET,
    priority: MaybeUnset[int | None] = UNSET,
    task_type: MaybeUnset[str | None] = UNSET,
    claimed_by_session_id: MaybeUnset[str | None] = UNSET,
    labels: MaybeUnset[list[str] | None] = UNSET,
    remove_labels: Sequence[str] = (),
    parent_task_id: MaybeUnset[str | None] = UNSET,
    closed_reason: MaybeUnset[str | None] = UNSET,
    closed_at: MaybeUnset[str | None] = UNSET,
    closed_in_session_id: MaybeUnset[str | None] = UNSET,
    closed_commit_sha: MaybeUnset[str | None] = UNSET,
    validation_status: MaybeUnset[str | None] = UNSET,
    validation_feedback: MaybeUnset[str | None] = UNSET,
    category: MaybeUnset[str | None] = UNSET,
    validation_criteria: MaybeUnset[str | None] = UNSET,
    validation_fail_count: MaybeUnset[int | None] = UNSET,
    dispatch_failure_count: MaybeUnset[int | None] = UNSET,
    merge_in_progress: MaybeUnset[bool] = UNSET,
    blocked_by_merge: MaybeUnset[bool] = UNSET,
    escalated_at: MaybeUnset[datetime | str | None] = UNSET,
    escalation_reason: MaybeUnset[str | None] = UNSET,
    github_issue_number: MaybeUnset[int | None] = UNSET,
    github_pr_number: MaybeUnset[int | None] = UNSET,
    github_repo: MaybeUnset[str | None] = UNSET,
    linear_issue_id: MaybeUnset[str | None] = UNSET,
    linear_team_id: MaybeUnset[str | None] = UNSET,
    validation_override_reason: MaybeUnset[str | None] = UNSET,
    allow_automation: MaybeUnset[bool] = UNSET,
    unattended: MaybeUnset[bool] = UNSET,
    yolo: MaybeUnset[bool] = UNSET,
    isolation: MaybeUnset[Isolation | str | None] = UNSET,
    assigned_agent: MaybeUnset[str | None] = UNSET,
    implementation_domain: MaybeUnset[str | None] = UNSET,
    additional_skills: MaybeUnset[list[str] | None] = UNSET,
    start_date: MaybeUnset[str | None] = UNSET,
    due_date: MaybeUnset[str | None] = UNSET,
    _require_escalated: bool = False,
) -> bool:
    """Internal storage primitive for task field updates.

    This function intentionally remains permissive because lifecycle and
    reconciliation helpers need an atomic write primitive below the manager
    policy boundary. External callers should use LocalTaskManager methods
    instead of calling this directly.

    Returns True if parent_task_id was changed (indicating path cache needs update).
    """
    current_task = get_task(db, task_id)
    updates: list[str] = []
    params: list[Any] = []
    now = utc_now()
    task_columns = table_column_names(db, "tasks")

    if title is not UNSET:
        updates.append("title = %s")
        params.append(title)
    if description is not UNSET:
        updates.append("description = %s")
        params.append(description)
    if priority is not UNSET:
        updates.append("priority = %s")
        params.append(priority)
    if task_type is not UNSET:
        updates.append("task_type = %s")
        params.append(validate_task_type(cast(str | None, task_type)))
    if claimed_by_session_id is not UNSET:
        updates.append("claimed_by_session_id = %s")
        params.append(claimed_by_session_id)
    if labels is not UNSET:
        if remove_labels:
            raise ValueError("labels and remove_labels cannot be updated together")
        updates.append("labels = %s")
        if labels is None:
            params.append("[]")
        else:
            params.append(json.dumps(labels))
    elif remove_labels:
        updates.append("labels = COALESCE(labels, '[]'::jsonb) - %s::text[]")
        params.append(list(remove_labels))
    if parent_task_id is not UNSET:
        updates.append("parent_task_id = %s")
        params.append(parent_task_id)
    if validation_status is not UNSET:
        updates.append("validation_status = %s")
        params.append(validation_status)
    if validation_feedback is not UNSET:
        updates.append("validation_feedback = %s")
        params.append(validation_feedback)
    if category is not UNSET:
        updates.append("category = %s")
        params.append(validate_category(cast(str | None, category)))
    if validation_criteria is not UNSET:
        updates.append("validation_criteria = %s")
        params.append(validation_criteria)
    if validation_fail_count is not UNSET:
        updates.append("validation_fail_count = %s")
        params.append(validation_fail_count)
    if dispatch_failure_count is not UNSET:
        updates.append("dispatch_failure_count = %s")
        params.append(dispatch_failure_count)
    if merge_in_progress is not UNSET:
        if merge_in_progress is None:
            raise ValueError("merge_in_progress cannot be None")
        updates.append("merge_in_progress = %s")
        params.append(bool(merge_in_progress))
    if blocked_by_merge is not UNSET:
        if blocked_by_merge is None:
            raise ValueError("blocked_by_merge cannot be None")
        updates.append("blocked_by_merge = %s")
        params.append(bool(blocked_by_merge))
    if github_issue_number is not UNSET:
        updates.append("github_issue_number = %s")
        params.append(github_issue_number)
    if github_pr_number is not UNSET:
        updates.append("github_pr_number = %s")
        params.append(github_pr_number)
    if github_repo is not UNSET:
        updates.append("github_repo = %s")
        params.append(github_repo)
    if linear_issue_id is not UNSET:
        updates.append("linear_issue_id = %s")
        params.append(linear_issue_id)
    if linear_team_id is not UNSET:
        updates.append("linear_team_id = %s")
        params.append(linear_team_id)
    if validation_override_reason is not UNSET:
        updates.append("validation_override_reason = %s")
        params.append(validation_override_reason)
    if allow_automation is not UNSET:
        updates.append("allow_automation = %s")
        params.append(bool(allow_automation))
    if unattended is UNSET and yolo is not UNSET:
        unattended = yolo
    if unattended is not UNSET:
        updates.append("unattended = %s")
        params.append(bool(unattended))
    if isolation is not UNSET:
        if isolation is None:
            raise ValueError("isolation cannot be None")
        updates.append("isolation = %s")
        params.append(Isolation(cast(str, isolation)).value)
    if assigned_agent is not UNSET:
        updates.append("assigned_agent = %s")
        params.append(assigned_agent)
    if implementation_domain is not UNSET:
        updates.append("implementation_domain = %s")
        params.append(validate_implementation_domain(cast(str | None, implementation_domain)))
    if additional_skills is not UNSET:
        updates.append("additional_skills = %s")
        params.append(json.dumps(additional_skills) if additional_skills is not None else None)
    if start_date is not UNSET:
        updates.append("start_date = %s")
        params.append(start_date)
    if due_date is not UNSET:
        updates.append("due_date = %s")
        params.append(due_date)
    next_closed_at = current_task.closed_at if closed_at is UNSET else cast(str | None, closed_at)
    next_escalated_at = (
        current_task.escalated_at
        if escalated_at is UNSET
        else cast(datetime | str | None, escalated_at)
    )

    if closed_at is not UNSET:
        updates.append("closed_at = %s")
        params.append(next_closed_at)
    if escalated_at is not UNSET:
        updates.append("escalated_at = %s")
        params.append(next_escalated_at)
        if "is_escalated" in task_columns:
            updates.append("is_escalated = %s")
            params.append(bool(next_escalated_at))

    if closed_reason is not UNSET:
        updates.append("closed_reason = %s")
        params.append(closed_reason)
    elif current_task.closed_at and next_closed_at is None:
        updates.append("closed_reason = %s")
        params.append(None)
    if closed_in_session_id is not UNSET:
        updates.append("closed_in_session_id = %s")
        params.append(closed_in_session_id)
    elif current_task.closed_at and next_closed_at is None:
        updates.append("closed_in_session_id = %s")
        params.append(None)
    if closed_commit_sha is not UNSET:
        updates.append("closed_commit_sha = %s")
        params.append(closed_commit_sha)
    elif current_task.closed_at and next_closed_at is None:
        updates.append("closed_commit_sha = %s")
        params.append(None)

    if escalation_reason is not UNSET:
        updates.append("escalation_reason = %s")
        params.append(escalation_reason)
    elif current_task.escalated_at and next_escalated_at is None:
        updates.append("escalation_reason = %s")
        params.append(None)

    if current_task.closed_at and next_closed_at is None:
        if validation_status is UNSET:
            updates.append("validation_status = %s")
            params.append(None)
        if validation_feedback is UNSET:
            updates.append("validation_feedback = %s")
            params.append(None)

    if not updates:
        return False

    updates.append("updated_at = %s")
    params.append(now)

    params.append(task_id)

    where_clause = "id = %s"
    if _require_escalated:
        where_clause += " AND is_escalated = TRUE"
    sql = f"UPDATE tasks SET {', '.join(updates)} WHERE {where_clause}"  # nosec B608

    with db.transaction() as conn:
        if parent_task_id is not UNSET:
            _validate_parent_task_id_update(conn, task_id, cast(str | None, parent_task_id))
        cursor = conn.execute(sql, tuple(params))
        if cursor.rowcount == 0:
            if _require_escalated:
                existing = conn.execute(
                    "SELECT 1 FROM tasks WHERE id = %s",
                    (task_id,),
                ).fetchone()
                if existing is not None:
                    raise ValueError(
                        "Cannot update escalation_reason for a task that is not escalated."
                    )
            raise ValueError(f"Task {task_id} not found")

    return parent_task_id is not UNSET


def update_task_metadata(
    db: HubDatabase,
    task_id: str,
    *,
    title: MaybeUnset[str | None] = UNSET,
    description: MaybeUnset[str | None] = UNSET,
    priority: MaybeUnset[int | None] = UNSET,
    task_type: MaybeUnset[str | None] = UNSET,
    claimed_by_session_id: MaybeUnset[str | None] = UNSET,
    labels: MaybeUnset[list[str] | None] = UNSET,
    parent_task_id: MaybeUnset[str | None] = UNSET,
    closed_reason: MaybeUnset[str | None] = UNSET,
    closed_at: MaybeUnset[str | None] = UNSET,
    closed_in_session_id: MaybeUnset[str | None] = UNSET,
    closed_commit_sha: MaybeUnset[str | None] = UNSET,
    validation_status: MaybeUnset[str | None] = UNSET,
    validation_feedback: MaybeUnset[str | None] = UNSET,
    category: MaybeUnset[str | None] = UNSET,
    validation_criteria: MaybeUnset[str | None] = UNSET,
    validation_fail_count: MaybeUnset[int | None] = UNSET,
    dispatch_failure_count: MaybeUnset[int | None] = UNSET,
    merge_in_progress: MaybeUnset[bool] = UNSET,
    blocked_by_merge: MaybeUnset[bool] = UNSET,
    escalated_at: MaybeUnset[datetime | str | None] = UNSET,
    escalation_reason: MaybeUnset[str | None] = UNSET,
    github_issue_number: MaybeUnset[int | None] = UNSET,
    github_pr_number: MaybeUnset[int | None] = UNSET,
    github_repo: MaybeUnset[str | None] = UNSET,
    linear_issue_id: MaybeUnset[str | None] = UNSET,
    linear_team_id: MaybeUnset[str | None] = UNSET,
    validation_override_reason: MaybeUnset[str | None] = UNSET,
    allow_automation: MaybeUnset[bool] = UNSET,
    unattended: MaybeUnset[bool] = UNSET,
    yolo: MaybeUnset[bool] = UNSET,
    isolation: MaybeUnset[Isolation | str | None] = UNSET,
    assigned_agent: MaybeUnset[str | None] = UNSET,
    implementation_domain: MaybeUnset[str | None] = UNSET,
    additional_skills: MaybeUnset[list[str] | None] = UNSET,
    start_date: MaybeUnset[str | None] = UNSET,
    due_date: MaybeUnset[str | None] = UNSET,
    **kwargs: Any,
) -> bool:
    """Validate a metadata-only update and dispatch to ``update_task``."""
    legacy_stage_key = "lifecycle_" + "stage"
    legacy_state_fields = sorted({"status", "lifecycle", legacy_stage_key} & set(kwargs))
    blocked_fields = [
        field_name
        for field_name, value in (
            ("claimed_by_session_id", claimed_by_session_id),
            ("closed_reason", closed_reason),
            ("closed_at", closed_at),
            ("closed_in_session_id", closed_in_session_id),
            ("closed_commit_sha", closed_commit_sha),
            ("escalated_at", escalated_at),
        )
        if value is not UNSET
    ]
    if legacy_state_fields or blocked_fields:
        blocked_display = ", ".join([*legacy_state_fields, *blocked_fields])
        if legacy_state_fields and not blocked_fields:
            field_class = "legacy state fields"
            transition_hint = (
                "Use start_stage, submit_for_review, approve_review, reject_review, "
                "fail_stage, close_task, reopen_task, or escalate_task instead."
            )
        else:
            field_class = "stage or ownership fields"
            transition_hint = (
                "Use claim_task, release_task_claim, start_stage, submit_for_review, "
                "approve_review, reject_review, fail_stage, escalate_task, "
                "de_escalate_task, close_task, or reopen_task instead."
            )
        raise ValueError(
            f"LocalTaskManager.update_task does not allow {field_class}. "
            f"{transition_hint} Blocked fields: {blocked_display}"
        )
    if kwargs:
        unsupported_display = ", ".join(sorted(kwargs))
        raise ValueError(
            f"LocalTaskManager.update_task received unsupported fields: {unsupported_display}"
        )

    return update_task(
        db,
        task_id=task_id,
        title=title,
        description=description,
        priority=priority,
        task_type=task_type,
        claimed_by_session_id=claimed_by_session_id,
        labels=labels,
        parent_task_id=parent_task_id,
        closed_reason=closed_reason,
        closed_at=closed_at,
        closed_in_session_id=closed_in_session_id,
        closed_commit_sha=closed_commit_sha,
        validation_status=validation_status,
        validation_feedback=validation_feedback,
        category=category,
        validation_criteria=validation_criteria,
        validation_fail_count=validation_fail_count,
        dispatch_failure_count=dispatch_failure_count,
        merge_in_progress=merge_in_progress,
        blocked_by_merge=blocked_by_merge,
        escalated_at=escalated_at,
        escalation_reason=escalation_reason,
        github_issue_number=github_issue_number,
        github_pr_number=github_pr_number,
        github_repo=github_repo,
        linear_issue_id=linear_issue_id,
        linear_team_id=linear_team_id,
        validation_override_reason=validation_override_reason,
        allow_automation=allow_automation,
        unattended=unattended,
        yolo=yolo,
        isolation=isolation,
        assigned_agent=assigned_agent,
        implementation_domain=implementation_domain,
        additional_skills=additional_skills,
        start_date=start_date,
        due_date=due_date,
        _require_escalated=escalation_reason is not UNSET,
    )
