"""Raw task update storage primitives."""

import json
from datetime import UTC, datetime
from typing import Any, cast

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import table_column_names
from gobby.storage.tasks._models import (
    UNSET,
    Isolation,
    MaybeUnset,
    validate_implementation_domain,
    validate_task_type,
)
from gobby.storage.tasks._ownership import _derive_claimed_by_session_id
from gobby.storage.tasks._read import get_task


def update_task(
    db: HubDatabase,
    task_id: str,
    title: MaybeUnset[str | None] = UNSET,
    description: MaybeUnset[str | None] = UNSET,
    priority: MaybeUnset[int | None] = UNSET,
    task_type: MaybeUnset[str | None] = UNSET,
    assignee: MaybeUnset[str | None] = UNSET,
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
    escalated_at: MaybeUnset[str | None] = UNSET,
    escalation_reason: MaybeUnset[str | None] = UNSET,
    github_issue_number: MaybeUnset[int | None] = UNSET,
    github_pr_number: MaybeUnset[int | None] = UNSET,
    github_repo: MaybeUnset[str | None] = UNSET,
    linear_issue_id: MaybeUnset[str | None] = UNSET,
    linear_team_id: MaybeUnset[str | None] = UNSET,
    validation_override_reason: MaybeUnset[str | None] = UNSET,
    allow_automation: MaybeUnset[bool | None] = UNSET,
    unattended: MaybeUnset[bool | None] = UNSET,
    yolo: MaybeUnset[bool | None] = UNSET,
    isolation: MaybeUnset[Isolation | str | None] = UNSET,
    assigned_agent: MaybeUnset[str | None] = UNSET,
    implementation_domain: MaybeUnset[str | None] = UNSET,
    additional_skills: MaybeUnset[list[str] | None] = UNSET,
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
    now = datetime.now(UTC).isoformat()
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
    if assignee is not UNSET:
        updates.append("assignee = %s")
        params.append(assignee)
    derived_claimed_by_session_id = _derive_claimed_by_session_id(
        db,
        assignee=assignee,
        claimed_by_session_id=claimed_by_session_id,
    )
    if derived_claimed_by_session_id is not UNSET:
        updates.append("claimed_by_session_id = %s")
        params.append(derived_claimed_by_session_id)
    if labels is not UNSET:
        updates.append("labels = %s")
        if labels is None:
            params.append("[]")
        else:
            params.append(json.dumps(labels))
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
        params.append(category)
    if validation_criteria is not UNSET:
        updates.append("validation_criteria = %s")
        params.append(validation_criteria)
    if validation_fail_count is not UNSET:
        updates.append("validation_fail_count = %s")
        params.append(validation_fail_count)
    if dispatch_failure_count is not UNSET:
        updates.append("dispatch_failure_count = %s")
        params.append(dispatch_failure_count)
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
    next_closed_at = current_task.closed_at if closed_at is UNSET else cast(str | None, closed_at)
    next_escalated_at = (
        current_task.escalated_at if escalated_at is UNSET else cast(str | None, escalated_at)
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

    sql = f"UPDATE tasks SET {', '.join(updates)} WHERE id = %s"  # nosec B608

    with db.transaction() as conn:
        cursor = conn.execute(sql, tuple(params))
        if cursor.rowcount == 0:
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
    assignee: MaybeUnset[str | None] = UNSET,
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
    escalated_at: MaybeUnset[str | None] = UNSET,
    escalation_reason: MaybeUnset[str | None] = UNSET,
    github_issue_number: MaybeUnset[int | None] = UNSET,
    github_pr_number: MaybeUnset[int | None] = UNSET,
    github_repo: MaybeUnset[str | None] = UNSET,
    linear_issue_id: MaybeUnset[str | None] = UNSET,
    linear_team_id: MaybeUnset[str | None] = UNSET,
    validation_override_reason: MaybeUnset[str | None] = UNSET,
    allow_automation: MaybeUnset[bool | None] = UNSET,
    unattended: MaybeUnset[bool | None] = UNSET,
    yolo: MaybeUnset[bool | None] = UNSET,
    isolation: MaybeUnset[Isolation | str | None] = UNSET,
    assigned_agent: MaybeUnset[str | None] = UNSET,
    implementation_domain: MaybeUnset[str | None] = UNSET,
    additional_skills: MaybeUnset[list[str] | None] = UNSET,
    **kwargs: Any,
) -> bool:
    """Validate a metadata-only update and dispatch to ``update_task``."""
    legacy_stage_key = "lifecycle_" + "stage"
    legacy_state_fields = sorted({"status", "lifecycle", legacy_stage_key} & set(kwargs))
    blocked_fields = [
        field_name
        for field_name, value in (
            ("assignee", assignee),
            ("claimed_by_session_id", claimed_by_session_id),
            ("closed_reason", closed_reason),
            ("closed_at", closed_at),
            ("closed_in_session_id", closed_in_session_id),
            ("closed_commit_sha", closed_commit_sha),
            ("escalated_at", escalated_at),
            ("escalation_reason", escalation_reason),
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

    return update_task(
        db,
        task_id=task_id,
        title=title,
        description=description,
        priority=priority,
        task_type=task_type,
        assignee=assignee,
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
    )
