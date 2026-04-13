import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._aggregates import (
    count_blocked_tasks as _count_blocked_tasks,
)
from gobby.storage.tasks._aggregates import (
    count_by_status as _count_by_status,
)
from gobby.storage.tasks._aggregates import (
    count_closed_since as _count_closed_since,
)
from gobby.storage.tasks._aggregates import (
    count_ready_tasks as _count_ready_tasks,
)
from gobby.storage.tasks._aggregates import (
    count_tasks as _count_tasks,
)
from gobby.storage.tasks._crud import (
    create_task as _create_task,
)
from gobby.storage.tasks._crud import (
    find_task_by_prefix as _find_task_by_prefix,
)
from gobby.storage.tasks._crud import (
    find_tasks_by_prefix as _find_tasks_by_prefix,
)
from gobby.storage.tasks._crud import (
    get_task as _get_task,
)
from gobby.storage.tasks._crud import (
    update_task as _update_task,
)
from gobby.storage.tasks._id import generate_task_id, resolve_task_reference
from gobby.storage.tasks._lifecycle import (
    add_label as _add_label,
)
from gobby.storage.tasks._lifecycle import (
    close_task as _close_task,
)
from gobby.storage.tasks._lifecycle import (
    delete_task as _delete_task,
)
from gobby.storage.tasks._lifecycle import (
    link_commit as _link_commit,
)
from gobby.storage.tasks._lifecycle import (
    remove_label as _remove_label,
)
from gobby.storage.tasks._lifecycle import (
    reopen_task as _reopen_task,
)
from gobby.storage.tasks._lifecycle import (
    unlink_commit as _unlink_commit,
)
from gobby.storage.tasks._models import (
    PRIORITY_MAP,
    UNSET,
    VALID_CATEGORIES,
    MaybeUnset,
    SeqNumCollisionError,
    Task,
    TaskIDCollisionError,
    TaskNotFoundError,
    UnsetType,
    normalize_priority,
    validate_category,
)
from gobby.storage.tasks._ordering import order_tasks_hierarchically
from gobby.storage.tasks._path_cache import (
    compute_path_cache,
    update_descendant_paths,
    update_path_cache,
)
from gobby.storage.tasks._queries import (
    list_blocked_tasks as _list_blocked_tasks,
)
from gobby.storage.tasks._queries import (
    list_ready_tasks as _list_ready_tasks,
)
from gobby.storage.tasks._queries import (
    list_tasks as _list_tasks,
)
from gobby.storage.tasks._search import TaskFTS5Searcher
from gobby.storage.tasks._transitions import (
    claim_task as _claim_task,
)
from gobby.storage.tasks._transitions import (
    de_escalate_task as _de_escalate_task,
)
from gobby.storage.tasks._transitions import (
    escalate_task as _escalate_task,
)
from gobby.storage.tasks._transitions import (
    mark_task_needs_review as _mark_task_needs_review,
)
from gobby.storage.tasks._transitions import (
    mark_task_review_approved as _mark_task_review_approved,
)
from gobby.storage.tasks._transitions import (
    release_task_claim as _release_task_claim,
)

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
__all__ = [
    "MaybeUnset",
    "PRIORITY_MAP",
    "UNSET",
    "VALID_CATEGORIES",
    "SeqNumCollisionError",
    "Task",
    "TaskIDCollisionError",
    "TaskNotFoundError",
    "UnsetType",
    "normalize_priority",
    "validate_category",
    "generate_task_id",
    "order_tasks_hierarchically",
    "LocalTaskManager",
]


class LocalTaskManager:
    def __init__(self, db: DatabaseProtocol):
        self.db = db
        self._change_listeners: list[Callable[[], Any]] = []
        self._searcher: TaskFTS5Searcher | None = None

    def add_change_listener(self, listener: Callable[[], Any]) -> None:
        """Add a listener to be called when tasks change."""
        self._change_listeners.append(listener)

    def _run_change_listeners(self) -> None:
        """Notify all listeners of a change."""
        for listener in self._change_listeners:
            try:
                listener()
            except Exception as e:
                logger.error(f"Error in task change listener: {e}")

    def _notify_listeners(self) -> None:
        """Notify listeners immediately or defer until the current commit."""
        after_commit = getattr(self.db, "after_commit", None)
        if callable(after_commit):
            after_commit(self._run_change_listeners)
            return
        self._run_change_listeners()

    def compute_path_cache(self, task_id: str) -> str | None:
        """Compute the hierarchical path for a task.

        Traverses up the parent chain to build a dotted path from seq_nums.
        Format: 'ancestor_seq.parent_seq.task_seq' (e.g., '1.3.47')

        Args:
            task_id: The task ID to compute path for

        Returns:
            Dotted path string (e.g., '1.3.47'), or None if task not found
            or any task in the chain is missing a seq_num.
        """
        return compute_path_cache(self.db, task_id)

    def update_path_cache(self, task_id: str) -> str | None:
        """Compute and store the path_cache for a task.

        Args:
            task_id: The task ID to update

        Returns:
            The computed path, or None if computation failed
        """
        return update_path_cache(self.db, task_id)

    def update_descendant_paths(self, task_id: str) -> int:
        """Update path_cache for a task and all its descendants.

        Use this after reparenting a task to cascade path updates.

        Args:
            task_id: The root task ID to start updating from

        Returns:
            Number of tasks updated
        """
        return update_descendant_paths(self.db, task_id)

    def create_task(
        self,
        project_id: str,
        title: str,
        description: str | None = None,
        parent_task_id: str | None = None,
        created_in_session_id: str | None = None,
        priority: int = 2,
        task_type: str = "task",
        assignee: str | None = None,
        claimed_by_session_id: str | None = None,
        lifecycle_stage: str | None = None,
        labels: list[str] | None = None,
        category: str | None = None,
        validation_criteria: str | None = None,
        github_issue_number: int | None = None,
        github_pr_number: int | None = None,
        github_repo: str | None = None,
        linear_issue_id: str | None = None,
        linear_team_id: str | None = None,
        **kwargs: Any,
    ) -> Task:
        """Create a new task with collision handling."""
        task_id = _create_task(
            self.db,
            project_id=project_id,
            title=title,
            description=description,
            parent_task_id=parent_task_id,
            created_in_session_id=created_in_session_id,
            priority=priority,
            task_type=task_type,
            assignee=assignee,
            claimed_by_session_id=claimed_by_session_id,
            lifecycle_stage=lifecycle_stage,
            labels=labels,
            category=category,
            validation_criteria=validation_criteria,
            github_issue_number=github_issue_number,
            github_pr_number=github_pr_number,
            github_repo=github_repo,
            linear_issue_id=linear_issue_id,
            linear_team_id=linear_team_id,
        )
        self._notify_listeners()
        return self.get_task(task_id)

    def get_task(self, task_id: str, project_id: str | None = None) -> Task:
        """Get a task by ID or reference.

        Accepts multiple formats:
          - UUID: Direct lookup
          - #N: Project-scoped seq_num (requires project_id)
          - N: Plain seq_num (requires project_id)

        Args:
            task_id: Task identifier in any supported format
            project_id: Required for #N and N formats

        Returns:
            The Task object

        Raises:
            ValueError: If task not found or format requires project_id
        """
        return _get_task(self.db, task_id, project_id)

    def find_task_by_prefix(self, prefix: str) -> Task | None:
        """Find a task by ID prefix. Returns None if no match or multiple matches."""
        return _find_task_by_prefix(self.db, prefix)

    def find_tasks_by_prefix(self, prefix: str) -> list[Task]:
        """Find all tasks matching an ID prefix."""
        return _find_tasks_by_prefix(self.db, prefix)

    def resolve_task_reference(self, ref: str, project_id: str) -> str:
        """Resolve a task reference to its UUID.

        Accepts multiple reference formats:
          - N: Plain seq_num (e.g., 47)
          - #N: Project-scoped seq_num (e.g., #47)
          - 1.2.3: Path cache format
          - UUID: Direct UUID (validated to exist)

        Args:
            ref: Task reference in any supported format
            project_id: Project ID for scoped lookups

        Returns:
            The task's UUID

        Raises:
            TaskNotFoundError: If the reference cannot be resolved
        """
        return resolve_task_reference(self.db, ref, project_id)

    def update_task(
        self,
        task_id: str,
        title: MaybeUnset[str | None] = UNSET,
        description: MaybeUnset[str | None] = UNSET,
        status: MaybeUnset[str | None] = UNSET,
        priority: MaybeUnset[int | None] = UNSET,
        task_type: MaybeUnset[str | None] = UNSET,
        assignee: MaybeUnset[str | None] = UNSET,
        claimed_by_session_id: MaybeUnset[str | None] = UNSET,
        lifecycle_stage: MaybeUnset[str | None] = UNSET,
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
        **kwargs: Any,
    ) -> Task:
        """Update task fields."""
        parent_changed = _update_task(
            self.db,
            task_id=task_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            task_type=task_type,
            assignee=assignee,
            claimed_by_session_id=claimed_by_session_id,
            lifecycle_stage=lifecycle_stage,
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
        )

        # If parent_task_id was changed, update path_cache for this task and all descendants
        if parent_changed:
            self.update_descendant_paths(task_id)

        self._notify_listeners()
        return self.get_task(task_id)

    def claim_task(self, task_id: str, session_id: str, force: bool = False) -> Task:
        """Claim a task for a session, preserving non-open lifecycle states."""
        task = _claim_task(self.db, task_id=task_id, session_id=session_id, force=force)
        self._notify_listeners()
        return task

    def release_task_claim(
        self,
        task_id: str,
        *,
        status: MaybeUnset[str | None] = UNSET,
        description: MaybeUnset[str | None] = UNSET,
        validation_fail_count: MaybeUnset[int | None] = UNSET,
        dispatch_failure_count: MaybeUnset[int | None] = UNSET,
        escalated_at: MaybeUnset[str | None] = UNSET,
        escalation_reason: MaybeUnset[str | None] = UNSET,
    ) -> Task:
        """Clear ownership while optionally changing lifecycle metadata."""
        task = _release_task_claim(
            self.db,
            task_id=task_id,
            status=status,
            description=description,
            validation_fail_count=validation_fail_count,
            dispatch_failure_count=dispatch_failure_count,
            escalated_at=escalated_at,
            escalation_reason=escalation_reason,
        )
        self._notify_listeners()
        return task

    def close_task(
        self,
        task_id: str,
        reason: str | None = None,
        force: bool = False,
        closed_in_session_id: str | None = None,
        closed_commit_sha: str | None = None,
        validation_override_reason: str | None = None,
    ) -> Task:
        """Close a task.

        Args:
            task_id: The task ID to close
            reason: Optional reason for closing
            force: If True, close even if there are open children (default: False)
            closed_in_session_id: Session ID where task was closed
            closed_commit_sha: Git commit SHA at time of closing
            validation_override_reason: Why agent bypassed validation (if applicable)

        Raises:
            ValueError: If task not found or has open children (and force=False)
        """
        _close_task(
            self.db,
            task_id=task_id,
            reason=reason,
            force=force,
            closed_in_session_id=closed_in_session_id,
            closed_commit_sha=closed_commit_sha,
            validation_override_reason=validation_override_reason,
        )
        self._notify_listeners()
        return self.get_task(task_id)

    def reopen_task(
        self,
        task_id: str,
        reason: str | None = None,
    ) -> Task:
        """Reopen a task to open status.

        Works on any non-open status. Clears assignee, closed fields,
        and resets validation_fail_count.

        Args:
            task_id: The task ID to reopen
            reason: Optional reason for reopening

        Raises:
            ValueError: If task not found or already open
        """
        _reopen_task(self.db, task_id=task_id, reason=reason)
        self._notify_listeners()
        return self.get_task(task_id)

    def escalate_task(
        self,
        task_id: str,
        reason: str,
        *,
        validation_override_reason: str | None = None,
    ) -> Task:
        """Escalate a task for human intervention.

        Optionally persists a validation override reason in the same write
        so callers don't need a follow-up update_task call.
        """
        task = _escalate_task(
            self.db,
            task_id=task_id,
            reason=reason,
            validation_override_reason=validation_override_reason,
        )
        self._notify_listeners()
        return task

    def de_escalate_task(
        self,
        task_id: str,
        reason: str,
        target_status: str | None = None,
        reset_validation: bool = False,
    ) -> Task:
        """Return an escalated task to an explicit next status."""
        task = _de_escalate_task(
            self.db,
            task_id=task_id,
            reason=reason,
            target_status=target_status,
            reset_validation=reset_validation,
        )
        self._notify_listeners()
        return task

    def mark_task_needs_review(self, task_id: str, review_notes: str | None = None) -> Task:
        """Mark a task as ready for review without dropping ownership."""
        task = _mark_task_needs_review(self.db, task_id=task_id, review_notes=review_notes)
        self._notify_listeners()
        return task

    def mark_task_review_approved(
        self,
        task_id: str,
        approval_notes: str | None = None,
    ) -> Task:
        """Mark a task as review-approved without dropping ownership."""
        task = _mark_task_review_approved(
            self.db,
            task_id=task_id,
            approval_notes=approval_notes,
        )
        self._notify_listeners()
        return task

    def add_label(self, task_id: str, label: str) -> Task:
        """Add a label to a task if not present."""
        result = _add_label(self.db, task_id, label)
        self._notify_listeners()
        return result

    def remove_label(self, task_id: str, label: str) -> Task:
        """Remove a label from a task if present."""
        result = _remove_label(self.db, task_id, label)
        self._notify_listeners()
        return result

    def link_commit(self, task_id: str, commit_sha: str, cwd: str | Path | None = None) -> Task:
        """Link a commit SHA to a task.

        Adds the commit SHA to the task's commits array if not already present.
        The SHA is normalized to dynamic short format for consistency.

        Args:
            task_id: The task ID to link the commit to.
            commit_sha: The git commit SHA to link (short or full).
            cwd: Working directory for git operations (defaults to current directory).

        Returns:
            Updated Task object.

        Raises:
            ValueError: If task not found or SHA cannot be resolved.
        """
        if _link_commit(self.db, task_id, commit_sha, cwd):
            self._notify_listeners()
        return self.get_task(task_id)

    def unlink_commit(self, task_id: str, commit_sha: str, cwd: str | Path | None = None) -> Task:
        """Unlink a commit SHA from a task.

        Removes the commit SHA from the task's commits array if present.
        Uses normalized SHA for exact matching.

        Args:
            task_id: The task ID to unlink the commit from.
            commit_sha: The git commit SHA to unlink (short or full).
            cwd: Working directory for git operations (defaults to current directory).

        Returns:
            Updated Task object.

        Raises:
            ValueError: If task not found.
        """
        if _unlink_commit(self.db, task_id, commit_sha, cwd):
            self._notify_listeners()
        return self.get_task(task_id)

    def delete_task(self, task_id: str, cascade: bool = False, unlink: bool = False) -> bool:
        """Delete a task.

        Args:
            task_id: The task ID to delete
            cascade: If True, delete children AND dependent tasks recursively
            unlink: If True, remove dependency links but preserve dependent tasks
                    (ignored if cascade=True)

        Returns:
            True if task was deleted, False if task not found.

        Raises:
            ValueError: If task has children or dependents and neither cascade nor unlink is True.
        """
        result = _delete_task(self.db, task_id, cascade=cascade, unlink=unlink)
        if result:
            self._notify_listeners()
        return result

    def list_tasks(
        self,
        project_id: str | None = None,
        status: str | list[str] | None = None,
        lifecycle_stage: str | list[str] | None = None,
        priority: int | None = None,
        assignee: str | None = None,
        claimed_by_session_id: str | None = None,
        claimed: bool | None = None,
        closed: bool | None = None,
        task_type: str | None = None,
        label: str | None = None,
        parent_task_id: str | None = None,
        title_like: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """List tasks with filtering.

        Args:
            status: Filter by status. Can be a single status string, a list of statuses,
                or None to include all statuses.

        Results are ordered hierarchically: parents appear before their children,
        with siblings sorted by priority ASC, then created_at ASC.
        """
        return _list_tasks(
            self.db,
            project_id=project_id,
            status=status,
            lifecycle_stage=lifecycle_stage,
            priority=priority,
            assignee=assignee,
            claimed_by_session_id=claimed_by_session_id,
            claimed=claimed,
            closed=closed,
            task_type=task_type,
            label=label,
            parent_task_id=parent_task_id,
            title_like=title_like,
            limit=limit,
            offset=offset,
        )

    def list_ready_tasks(
        self,
        project_id: str | None = None,
        priority: int | None = None,
        task_type: str | None = None,
        assignee: str | None = None,
        parent_task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """List tasks that are ready to work on (open or in_progress) and not blocked.

        A task is ready if:
        1. It is open or in_progress
        2. It has no open blocking dependencies
        3. Its parent (if any) is also ready (recursive check up the chain)

        Note: in_progress tasks are included because they represent active work
        that should remain visible in the ready queue.

        Results are ordered hierarchically: parents appear before their children,
        with siblings sorted by priority ASC, then created_at ASC.
        """
        return _list_ready_tasks(
            self.db,
            project_id=project_id,
            priority=priority,
            task_type=task_type,
            assignee=assignee,
            parent_task_id=parent_task_id,
            limit=limit,
            offset=offset,
        )

    def list_blocked_tasks(
        self,
        project_id: str | None = None,
        parent_task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """List tasks that are blocked by at least one open blocking dependency.

        Only considers "external" blockers - excludes parent tasks being blocked
        by their own descendants (which is a "completion" block, not a "work" block).

        Results are ordered hierarchically: parents appear before their children,
        with siblings sorted by priority ASC, then created_at ASC.
        """
        return _list_blocked_tasks(
            self.db,
            project_id=project_id,
            parent_task_id=parent_task_id,
            limit=limit,
            offset=offset,
        )

    def count_tasks(
        self,
        project_id: str | None = None,
        status: str | None = None,
    ) -> int:
        """Count tasks with optional filters.

        Args:
            project_id: Filter by project
            status: Filter by status

        Returns:
            Count of matching tasks
        """
        return _count_tasks(self.db, project_id=project_id, status=status)

    def count_by_status(self, project_id: str | None = None) -> dict[str, int]:
        """Count tasks grouped by status.

        Args:
            project_id: Optional project filter

        Returns:
            Dictionary mapping status to count
        """
        return _count_by_status(self.db, project_id=project_id)

    def count_ready_tasks(self, project_id: str | None = None) -> int:
        """Count tasks that are ready (open or in_progress) and not blocked.

        A task is ready if it has no external blocking dependencies.
        Excludes parent tasks blocked by their own descendants (completion block, not work block).

        Args:
            project_id: Optional project filter

        Returns:
            Count of ready tasks
        """
        return _count_ready_tasks(self.db, project_id=project_id)

    def count_blocked_tasks(self, project_id: str | None = None) -> int:
        """Count tasks that are blocked by at least one external blocking dependency.

        Excludes parent tasks blocked by their own descendants (completion block, not work block).

        Args:
            project_id: Optional project filter

        Returns:
            Count of blocked tasks
        """
        return _count_blocked_tasks(self.db, project_id=project_id)

    def count_closed_since(self, hours: int = 24, project_id: str | None = None) -> int:
        """Count tasks closed within the last N hours."""
        return _count_closed_since(self.db, hours=hours, project_id=project_id)

    def create_task_with_decomposition(
        self,
        project_id: str,
        title: str,
        description: str | None = None,
        parent_task_id: str | None = None,
        created_in_session_id: str | None = None,
        priority: int = 2,
        task_type: str = "task",
        assignee: str | None = None,
        labels: list[str] | None = None,
        category: str | None = None,
        validation_criteria: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a task and return result dict."""
        task = self.create_task(
            project_id=project_id,
            title=title,
            description=description,
            parent_task_id=parent_task_id,
            created_in_session_id=created_in_session_id,
            priority=priority,
            task_type=task_type,
            assignee=assignee,
            labels=labels,
            category=category,
            validation_criteria=validation_criteria,
        )
        return {"task": task.to_dict()}

    def update_task_with_result(
        self,
        task_id: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update a task's description and return result dict.

        Args:
            task_id: Task ID
            description: New description

        Returns:
            Dict with task details.
        """
        updated = self.update_task(task_id, description=description)
        return {"task": updated.to_dict()}

    # --- Search Methods ---

    def _ensure_searcher(self) -> TaskFTS5Searcher:
        """Get or create the task searcher instance."""
        if self._searcher is None:
            self._searcher = TaskFTS5Searcher(self.db)
        return self._searcher

    def search_tasks(
        self,
        query: str,
        project_id: str | None = None,
        status: str | list[str] | None = None,
        task_type: str | None = None,
        priority: int | None = None,
        parent_task_id: str | None = None,
        category: str | None = None,
        limit: int = 20,
        min_score: float = 0.0,
    ) -> list[tuple[Task, float]]:
        """Search tasks using FTS5 full-text search.

        Single-query search with SQL filter push-down — all filters
        are applied in the FTS5 JOIN query.

        Args:
            query: Search query text
            project_id: Filter by project
            status: Filter by status (string or list of strings)
            task_type: Filter by task type
            priority: Filter by priority
            parent_task_id: Filter by parent task ID (UUID)
            category: Filter by task category
            limit: Maximum results to return
            min_score: Minimum similarity score threshold (0.0-1.0)

        Returns:
            List of (Task, similarity_score) tuples, sorted by score descending
        """
        searcher = self._ensure_searcher()

        search_results = searcher.search(
            query,
            top_k=limit,
            project_id=project_id,
            status=status,
            task_type=task_type,
            priority=priority,
            parent_task_id=parent_task_id,
            category=category,
            min_score=min_score,
        )

        if not search_results:
            return []

        # Batch-fetch tasks for the result set
        results: list[tuple[Task, float]] = []
        for task_id, score in search_results:
            try:
                task = self.get_task(task_id)
            except (ValueError, TaskNotFoundError):
                continue
            results.append((task, score))

        return results

    def reindex_search(self, project_id: str | None = None) -> dict[str, Any]:
        """Force rebuild of the FTS5 task search index.

        Normally triggers keep the index in sync. Use this for repair.

        Args:
            project_id: Unused - kept for API compatibility.

        Returns:
            Dict with index statistics
        """
        searcher = self._ensure_searcher()
        return searcher.reindex()
