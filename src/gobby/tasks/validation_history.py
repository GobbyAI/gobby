"""Read-only access to historical validation iterations."""

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.failure_categories import FailureCategory
from gobby.tasks.validation_models import Issue

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def _safe_failure_category(value: object) -> FailureCategory | None:
    if value is None:
        return None
    if not isinstance(value, str):
        logger.warning(
            "Ignoring non-string validation failure category", extra={"value": repr(value)}
        )
        return None
    try:
        return FailureCategory(value)
    except ValueError:
        logger.warning("Ignoring unknown validation failure category", extra={"value": value})
        return None


@dataclass
class ValidationIteration:
    """Represents a single validation iteration for a task.

    Attributes:
        id: Database ID of the iteration record
        task_id: ID of the task being validated
        iteration: Iteration number (1-based)
        status: Validation status ("valid", "invalid", "error")
        feedback: Human-readable feedback from validator
        issues: List of Issue objects found during validation
        context_type: Type of context provided (e.g., "git_diff", "code_review")
        context_summary: Summary of the context provided
        validator_type: Type of validator used (e.g., "llm", "external_webhook")
        created_at: Timestamp when iteration was recorded
    """

    id: int
    task_id: str
    iteration: int
    status: str
    feedback: str | None = None
    issues: list[Issue] | None = None
    context_type: str | None = None
    context_summary: str | None = None
    validator_type: str | None = None
    failure_category: FailureCategory | None = None
    created_at: str | None = None


class ValidationHistoryManager:
    """Read immutable validation iterations archived before automatic review retired."""

    def __init__(self, db: "HubDatabase"):
        """Initialize ValidationHistoryManager.

        Args:
            db: HubDatabase instance for database operations.
        """
        self.db = db

    def get_iteration_history(self, task_id: str) -> list[ValidationIteration]:
        """Get all validation iterations for a task.

        Args:
            task_id: ID of the task to get history for.

        Returns:
            List of ValidationIteration objects ordered by iteration number.
        """
        rows = self.db.fetchall(
            """SELECT * FROM task_validation_history
               WHERE task_id = %s
               ORDER BY iteration ASC, id ASC""",
            (task_id,),
        )

        return [self._row_to_iteration(row) for row in rows]

    def get_latest_iteration(self, task_id: str) -> ValidationIteration | None:
        """Get the most recent validation iteration for a task.

        Args:
            task_id: ID of the task to get latest iteration for.

        Returns:
            Latest ValidationIteration or None if no history exists.
        """
        row = self.db.fetchone(
            """SELECT * FROM task_validation_history
               WHERE task_id = %s
               ORDER BY iteration DESC, id DESC
               LIMIT 1""",
            (task_id,),
        )

        if row:
            return self._row_to_iteration(row)
        return None

    def _row_to_iteration(self, row: Any) -> ValidationIteration:
        """Convert a database row to a ValidationIteration object.

        Args:
            row: Database row from task_validation_history.

        Returns:
            ValidationIteration object.
        """
        # Parse issues from JSON
        issues = None
        issues_json = row["issues"]
        if issues_json:
            issues_data = json.loads(issues_json)
            issues = [Issue.from_dict(d) for d in issues_data]

        return ValidationIteration(
            id=row["id"],
            task_id=row["task_id"],
            iteration=row["iteration"],
            status=row["status"],
            feedback=row["feedback"],
            issues=issues,
            context_type=row["context_type"],
            context_summary=row["context_summary"],
            validator_type=row["validator_type"],
            failure_category=_safe_failure_category(row["failure_category"]),
            created_at=row["created_at"],
        )
