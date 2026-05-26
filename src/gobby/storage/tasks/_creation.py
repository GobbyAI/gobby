"""Task creation storage primitive."""

import json
import logging
from datetime import UTC, datetime

import psycopg
from psycopg.errors import UniqueViolation

from gobby.storage.hub.protocol import HubDatabase, TaskSeqAllocation, Transaction
from gobby.storage.tasks._id import generate_task_id
from gobby.storage.tasks._models import (
    UNSET,
    SeqNumCollisionError,
    TaskIDCollisionError,
    validate_implementation_domain,
    validate_task_type,
)
from gobby.storage.tasks._ownership import _derive_claimed_by_session_id

logger = logging.getLogger(__name__)


def create_task(
    db: HubDatabase,
    project_id: str,
    title: str,
    description: str | None = None,
    parent_task_id: str | None = None,
    created_in_session_id: str | None = None,
    priority: int = 2,
    task_type: str = "task",
    assignee: str | None = None,
    claimed_by_session_id: str | None = None,
    labels: list[str] | None = None,
    category: str | None = None,
    validation_criteria: str | None = None,
    assigned_agent: str | None = None,
    implementation_domain: str | None = None,
    additional_skills: list[str] | None = None,
    github_issue_number: int | None = None,
    github_pr_number: int | None = None,
    github_repo: str | None = None,
    linear_issue_id: str | None = None,
    linear_team_id: str | None = None,
) -> str:
    """Create a new task with collision handling.

    Returns the task_id of the created task.
    """
    with db.transaction_immediate(TaskSeqAllocation(project_id=project_id)) as conn:
        return _create_task_in_transaction(
            db,
            conn,
            project_id=project_id,
            title=title,
            description=description,
            parent_task_id=parent_task_id,
            created_in_session_id=created_in_session_id,
            priority=priority,
            task_type=task_type,
            assignee=assignee,
            claimed_by_session_id=claimed_by_session_id,
            labels=labels,
            category=category,
            validation_criteria=validation_criteria,
            assigned_agent=assigned_agent,
            implementation_domain=implementation_domain,
            additional_skills=additional_skills,
            github_issue_number=github_issue_number,
            github_pr_number=github_pr_number,
            github_repo=github_repo,
            linear_issue_id=linear_issue_id,
            linear_team_id=linear_team_id,
        )


def _create_task_in_transaction(
    db: HubDatabase,
    conn: Transaction,
    *,
    project_id: str,
    title: str,
    description: str | None = None,
    parent_task_id: str | None = None,
    created_in_session_id: str | None = None,
    priority: int = 2,
    task_type: str = "task",
    assignee: str | None = None,
    claimed_by_session_id: str | None = None,
    labels: list[str] | None = None,
    category: str | None = None,
    validation_criteria: str | None = None,
    assigned_agent: str | None = None,
    implementation_domain: str | None = None,
    additional_skills: list[str] | None = None,
    github_issue_number: int | None = None,
    github_pr_number: int | None = None,
    github_repo: str | None = None,
    linear_issue_id: str | None = None,
    linear_team_id: str | None = None,
) -> str:
    """Insert a task using a caller-owned TaskSeqAllocation transaction."""
    max_retries = 3
    now = datetime.now(UTC).isoformat()

    labels_json = json.dumps(labels) if labels else None
    additional_skills_json = (
        json.dumps(additional_skills) if additional_skills is not None else None
    )
    task_id = ""

    task_type = validate_task_type(task_type)
    implementation_domain = validate_implementation_domain(implementation_domain)
    validation_status = "pending" if validation_criteria else None
    canonical_owner = _derive_claimed_by_session_id(
        db,
        assignee=assignee,
        claimed_by_session_id=claimed_by_session_id,
    )
    if canonical_owner is UNSET:
        canonical_owner = None

    for attempt in range(max_retries + 1):
        savepoint = conn.savepoint(f"task_create_attempt_{attempt}")
        try:
            task_id = generate_task_id(project_id, salt=str(attempt))

            max_seq_row = conn.execute(
                "SELECT MAX(seq_num) as max_seq FROM tasks WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            next_seq_num = ((max_seq_row["max_seq"] if max_seq_row else None) or 0) + 1

            conn.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, parent_task_id,
                    created_in_session_id, claimed_by_session_id,
                    priority, task_type, assignee,
                    labels, created_at, updated_at,
                    validation_status, category,
                    validation_criteria, validation_fail_count,
                    assigned_agent, implementation_domain, additional_skills,
                    github_issue_number, github_pr_number, github_repo,
                    linear_issue_id, linear_team_id, seq_num
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    project_id,
                    title,
                    description,
                    parent_task_id,
                    created_in_session_id,
                    canonical_owner,
                    priority,
                    task_type,
                    assignee,
                    labels_json,
                    now,
                    now,
                    validation_status,
                    category,
                    validation_criteria,
                    assigned_agent,
                    implementation_domain,
                    additional_skills_json,
                    github_issue_number,
                    github_pr_number,
                    github_repo,
                    linear_issue_id,
                    linear_team_id,
                    next_seq_num,
                ),
            )

            logger.debug("Created task %s in project %s", task_id, project_id)

            path_parts: list[str] = [str(next_seq_num)]
            current_parent = parent_task_id
            max_depth = 100
            depth = 0
            while current_parent and depth < max_depth:
                parent_row = conn.execute(
                    "SELECT seq_num, parent_task_id FROM tasks WHERE id = ?",
                    (current_parent,),
                ).fetchone()
                if not parent_row or parent_row["seq_num"] is None:
                    break
                path_parts.append(str(parent_row["seq_num"]))
                current_parent = parent_row["parent_task_id"]
                depth += 1

            path_parts.reverse()
            path_cache = ".".join(path_parts)
            conn.execute(
                "UPDATE tasks SET path_cache = ? WHERE id = ?",
                (path_cache, task_id),
            )

            savepoint.release()
            return task_id

        except (psycopg.IntegrityError, UniqueViolation) as e:
            savepoint.rollback()
            savepoint.release()
            error_msg = str(e)
            if "tasks.id" in error_msg or "tasks_pkey" in error_msg or "Key (id)=" in error_msg:
                if attempt == max_retries:
                    raise TaskIDCollisionError(
                        f"Failed to generate unique task ID after {max_retries} retries"
                    ) from e
                logger.warning("Task ID collision for %s, retrying...", task_id)
                continue
            if (
                "idx_tasks_seq_num" in error_msg
                or "tasks.seq_num" in error_msg
                or "seq_num" in error_msg
            ):
                if attempt == max_retries:
                    raise SeqNumCollisionError(
                        f"Failed to allocate unique seq_num after {max_retries} retries"
                    ) from e
                logger.warning("Task seq_num collision for project %s, retrying...", project_id)
                continue
            raise

    raise TaskIDCollisionError("Unreachable")
