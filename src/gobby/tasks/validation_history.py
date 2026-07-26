"""Validation history management for Task System V2.

Provides storage and retrieval of validation iteration history.
"""

import json
import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
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
        logger.warning("Ignoring non-string validation failure category", extra={"value": repr(value)})
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
    """Manages validation iteration history for tasks.

    Stores and retrieves validation history from the task_validation_history table.
    """

    def __init__(self, db: "HubDatabase"):
        """Initialize ValidationHistoryManager.

        Args:
            db: HubDatabase instance for database operations.
        """
        self.db = db

    def record_iteration(
        self,
        task_id: str,
        iteration: int,
        status: str,
        feedback: str | None = None,
        issues: list[Issue] | None = None,
        context_type: str | None = None,
        context_summary: str | None = None,
        validator_type: str | None = None,
        failure_category: FailureCategory | None = None,
    ) -> None:
        """Record a validation iteration for a task.

        Args:
            task_id: ID of the task being validated.
            iteration: Iteration number (1-based).
            status: Validation status ("valid", "invalid", "error").
            feedback: Human-readable feedback from validator.
            issues: List of Issue objects found during validation.
            context_type: Type of context provided.
            context_summary: Summary of the context provided.
            validator_type: Type of validator used.
        """
        # Serialize issues to JSON
        issues_json = None
        if issues:
            issues_json = json.dumps([issue.to_dict() for issue in issues])

        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO task_validation_history
                   (task_id, iteration, status, feedback, issues, context_type,
                    context_summary, validator_type, failure_category)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    task_id,
                    iteration,
                    status,
                    feedback,
                    issues_json,
                    context_type,
                    context_summary,
                    validator_type,
                    failure_category.value if failure_category is not None else None,
                ),
            )

        logger.debug("Recorded validation iteration %s for task %s: %s", iteration, task_id, status)

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

    def clear_history(self, task_id: str) -> None:
        """Remove all validation history for a task.

        Args:
            task_id: ID of the task to clear history for.
        """
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM task_validation_history WHERE task_id = %s",
                (task_id,),
            )

        logger.debug("Cleared validation history for task %s", task_id)

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

    # =========================================================================
    # Recurring Issue Detection
    # =========================================================================

    def group_similar_issues(
        self,
        issues: list[Issue],
        similarity_threshold: float = 0.8,
    ) -> list[list[Issue]]:
        """Group issues by similarity.

        Uses fuzzy string matching on titles and exact matching on locations.
        Issues at the same location are always grouped together.

        Args:
            issues: List of Issue objects to group.
            similarity_threshold: Minimum similarity ratio (0-1) for title matching.

        Returns:
            List of groups, where each group is a list of similar Issues.
        """
        if not issues:
            return []

        groups: list[list[Issue]] = []
        used: set[int] = set()

        for i, issue in enumerate(issues):
            if i in used:
                continue

            # Start a new group with this issue
            group = [issue]
            used.add(i)

            # Find similar issues
            for j, other in enumerate(issues):
                if j in used:
                    continue

                if self._issues_are_similar(issue, other, similarity_threshold):
                    group.append(other)
                    used.add(j)

            groups.append(group)

        return groups

    def _issues_are_similar(
        self,
        issue1: Issue,
        issue2: Issue,
        threshold: float,
    ) -> bool:
        """Check if two issues are similar.

        Args:
            issue1: First issue to compare.
            issue2: Second issue to compare.
            threshold: Minimum similarity ratio for title matching.

        Returns:
            True if issues are considered similar.
        """
        # Same location is a strong match signal
        if issue1.location and issue2.location and issue1.location == issue2.location:
            return True

        # Check title similarity using SequenceMatcher
        ratio = SequenceMatcher(None, issue1.title.lower(), issue2.title.lower()).ratio()
        return ratio >= threshold

    def has_recurring_issues(
        self,
        task_id: str,
        threshold: int = 2,
        similarity_threshold: float = 0.8,
    ) -> bool:
        """Check if a task has recurring issues across iterations.

        Args:
            task_id: ID of the task to check.
            threshold: Minimum number of occurrences to consider recurring.
            similarity_threshold: Minimum similarity ratio for grouping issues.

        Returns:
            True if any issue recurs at least `threshold` times.
        """
        summary = self.get_recurring_issue_summary(
            task_id,
            threshold=threshold,
            similarity_threshold=similarity_threshold,
        )
        return bool(summary["recurring_issues"])

    def _group_failing_iteration_issues(
        self,
        history: list[ValidationIteration],
        similarity_threshold: float,
    ) -> list[list[tuple[int, Issue]]]:
        """Group issues while retaining distinct failing-iteration provenance."""
        groups: list[list[tuple[int, Issue]]] = []
        for iteration in history:
            if iteration.status != "invalid" or not iteration.issues:
                continue

            unique_issues: list[Issue] = []
            for issue in iteration.issues:
                if not any(
                    self._issues_are_similar(issue, existing, similarity_threshold)
                    for existing in unique_issues
                ):
                    unique_issues.append(issue)

            for issue in unique_issues:
                for group in groups:
                    if self._issues_are_similar(issue, group[0][1], similarity_threshold):
                        group.append((iteration.iteration, issue))
                        break
                else:
                    groups.append([(iteration.iteration, issue)])
        return groups

    def get_recurring_issue_summary(
        self,
        task_id: str,
        threshold: int = 2,
        similarity_threshold: float = 0.8,
    ) -> dict[str, Any]:
        """Get a summary of recurring issues for a task.

        Args:
            task_id: ID of the task to analyze.
            threshold: Minimum occurrences to consider an issue recurring.
            similarity_threshold: Minimum similarity ratio for grouping.

        Returns:
            Dictionary with:
                - recurring_issues: List of recurring issue summaries
                - total_iterations: Total number of validation iterations
        """
        history = self.get_iteration_history(task_id)

        if not history:
            return {
                "recurring_issues": [],
                "total_iterations": 0,
            }

        groups = self._group_failing_iteration_issues(history, similarity_threshold)

        recurring_issues: list[dict[str, Any]] = []
        for group in groups:
            iteration_ids = sorted({iteration_id for iteration_id, _issue in group})
            if len(iteration_ids) >= threshold:
                representative = group[0][1]
                titles = sorted({issue.title for _iteration_id, issue in group})
                anchors = sorted(
                    {
                        issue.location.strip()
                        for _iteration_id, issue in group
                        if issue.location and issue.location.strip()
                    }
                )
                recurring_issues.append(
                    {
                        "title": representative.title,
                        "titles": titles,
                        "type": representative.issue_type.value,
                        "severity": representative.severity.value,
                        "location": anchors[0] if anchors else None,
                        "anchors": anchors,
                        "count": len(iteration_ids),
                        "distinct_iteration_count": len(iteration_ids),
                        "failed_iterations": iteration_ids,
                    }
                )

        recurring_issues.sort(
            key=lambda issue: (
                -int(issue["distinct_iteration_count"]),
                str(issue["title"]).casefold(),
            )
        )

        return {
            "recurring_issues": recurring_issues,
            "total_iterations": len(history),
        }
