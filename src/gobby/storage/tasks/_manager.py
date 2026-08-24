import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.storage.tasks._aggregates import (
    count_blocked_tasks as _count_blocked_tasks,
)
from gobby.storage.tasks._aggregates import (
    count_by_state as _count_by_state,
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
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._blocking import hydrate_task_blocking_state
from gobby.storage.tasks._build_cascade import (
    CascadeBuildResult,
)
from gobby.storage.tasks._build_cascade import (
    cascade_build_state_to_subtree as _cascade_build_state_to_subtree,
)
from gobby.storage.tasks._creation import (
    create_task as _create_task,
)
from gobby.storage.tasks._decomposition import TaskDecompositionMixin
from gobby.storage.tasks._guard_scope import (
    list_epic_guard_scope as _list_epic_guard_scope,
)
from gobby.storage.tasks._id import generate_task_id, resolve_task_reference
from gobby.storage.tasks._lifecycle import (
    add_label as _add_label,
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
    unlink_commit as _unlink_commit,
)
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._models import (
    PRIORITY_MAP,
    UNSET,
    VALID_CATEGORIES,
    Isolation,
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
from gobby.storage.tasks._read import (
    find_task_by_prefix as _find_task_by_prefix,
)
from gobby.storage.tasks._read import (
    find_tasks_by_prefix as _find_tasks_by_prefix,
)
from gobby.storage.tasks._read import (
    get_task as _get_task,
)
from gobby.storage.tasks._search import TaskSearchBackend
from gobby.storage.tasks._stage_hydration import hydrate_task_stage_state
from gobby.storage.tasks._stage_manifest import initialize_task_manifest_for_task
from gobby.storage.tasks._stage_registry import StageRegistryManager
from gobby.storage.tasks._stage_states import StageStatesManager
from gobby.storage.tasks._transitions_facade import TaskTransitionsMixin
from gobby.storage.tasks._updates import (
    update_task_metadata as _update_task_metadata,
)
from gobby.tasks.criteria_contract import require_validation_criteria

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


class LocalTaskManager(TaskTransitionsMixin, TaskDecompositionMixin):
    def __init__(self, db: HubDatabase):
        self.db = db
        self._change_listeners: list[Callable[[], Any]] = []
        self._searcher: TaskSearchBackend | None = None
        self._artifact_manager: TaskArtifactManager | None = None
        self._lifecycle_event_manager: TaskLifecycleEventManager | None = None
        self._stage_registry_manager: StageRegistryManager | None = None
        self._stage_states_manager: StageStatesManager | None = None

    @property
    def artifacts(self) -> TaskArtifactManager:
        if self._artifact_manager is None:
            self._artifact_manager = TaskArtifactManager(self.db)
        return self._artifact_manager

    @property
    def lifecycle_events(self) -> TaskLifecycleEventManager:
        if self._lifecycle_event_manager is None:
            self._lifecycle_event_manager = TaskLifecycleEventManager(self.db)
        return self._lifecycle_event_manager

    @property
    def stages_registry(self) -> StageRegistryManager:
        if self._stage_registry_manager is None:
            self._stage_registry_manager = StageRegistryManager(self.db)
        return self._stage_registry_manager

    @property
    def stage_states(self) -> StageStatesManager:
        if self._stage_states_manager is None:
            self._stage_states_manager = StageStatesManager(self.db, self.lifecycle_events)
        return self._stage_states_manager

    def add_change_listener(self, listener: Callable[[], Any]) -> None:
        """Add a listener to be called when tasks change."""
        self._change_listeners.append(listener)

    def _run_change_listeners(self) -> None:
        """Notify all listeners of a change."""
        for listener in self._change_listeners:
            try:
                listener()
            except Exception as e:
                logger.error("Error in task change listener: %s", e)

    def _notify_listeners(self) -> None:
        """Notify listeners immediately or defer until the current commit."""
        after_commit = getattr(self.db, "after_commit", None)
        if callable(after_commit):
            after_commit(self._run_change_listeners)
            return
        self._run_change_listeners()

    def compute_path_cache(self, task_id: str) -> str | None:
        """Compute the hierarchical dotted path for a task."""
        return compute_path_cache(self.db, task_id)

    def update_path_cache(self, task_id: str) -> str | None:
        """Compute and store the path_cache for a task."""
        return update_path_cache(self.db, task_id)

    def update_descendant_paths(self, task_id: str) -> int:
        """Update path_cache for a task and all descendants."""
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
        self._notify_listeners()
        return self.get_task(task_id)

    def initialize_task_manifest(
        self,
        task_id: str,
        *,
        task_type: str | None = None,
        stage_names: Sequence[str] | None = None,
        stage_caps: Sequence[Mapping[str, object]] | None = None,
        by_session_id: str | None = None,
    ) -> list[Any]:
        """Explicitly initialize a task lifecycle manifest."""
        resolved_type = task_type or self.get_task(task_id).task_type
        rows = initialize_task_manifest_for_task(
            self.stages_registry,
            self.stage_states,
            task_id,
            task_type=resolved_type,
            stage_names=stage_names,
            stage_caps=stage_caps,
            by_session_id=by_session_id,
        )
        self._notify_listeners()
        return rows

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
        affected_files: MaybeUnset[list[str]] = UNSET,
        **kwargs: Any,
    ) -> Task:
        """Update metadata fields and optionally replace declared file scope.

        Stage and task/session ownership mutations must go through the dedicated task
        transition methods so claim/session state stays coherent.
        """
        if task_type is not UNSET or validation_criteria is not UNSET:
            current_task = self.get_task(task_id)
            effective_task_type = (
                current_task.task_type if task_type is UNSET else str(task_type or "")
            )
            if validation_criteria is UNSET:
                effective_criteria = current_task.validation_criteria
            else:
                effective_criteria = (
                    validation_criteria if isinstance(validation_criteria, str) else None
                )
            effective_criteria = require_validation_criteria(
                effective_task_type,
                effective_criteria,
            )
            if validation_criteria is not UNSET:
                validation_criteria = effective_criteria

        with self.db.transaction():
            parent_changed = _update_task_metadata(
                self.db,
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
                **kwargs,
            )

            if parent_changed:
                self.update_descendant_paths(task_id)

            if affected_files is not UNSET:
                TaskAffectedFileManager(self.db).replace_declared_files(
                    task_id,
                    cast(list[str], affected_files),
                )

        self._notify_listeners()
        return self.get_task(task_id)

    def set_merge_status(
        self,
        task_id: str,
        *,
        merge_in_progress: bool,
        blocked_by_merge: bool,
    ) -> Task:
        """Persist task-level merge status flags."""
        _update_task_metadata(
            self.db,
            task_id=task_id,
            merge_in_progress=merge_in_progress,
            blocked_by_merge=blocked_by_merge,
        )
        self._notify_listeners()
        return self.get_task(task_id)

    def cascade_build_state_to_subtree(
        self,
        epic_id: str,
        isolation: Isolation | str,
        unattended: bool | None,
        allow_automation: bool,
        *,
        skip_stages: Iterable[str] = (),
        yolo: bool | None = None,
        parent_manifest_specs: Iterable[Any] | None = None,
        include_merge_stage: bool = False,
    ) -> CascadeBuildResult:
        """Apply build dispatch state to an epic and every descendant task."""
        if unattended is None:
            unattended = bool(yolo)
        result = _cascade_build_state_to_subtree(
            self.db,
            epic_id=epic_id,
            isolation=isolation,
            unattended=unattended,
            allow_automation=allow_automation,
            skip_stages=skip_stages,
            parent_manifest_specs=parent_manifest_specs,
            include_merge_stage=include_merge_stage,
        )
        self._notify_listeners()
        return result

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
        """Add ``commit_sha`` to the task's commits array (normalized to short SHA)."""
        if _link_commit(self.db, task_id, commit_sha, cwd):
            self._notify_listeners()
        return self.get_task(task_id)

    def unlink_commit(self, task_id: str, commit_sha: str, cwd: str | Path | None = None) -> Task:
        """Remove ``commit_sha`` from the task's commits array if present."""
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
        current_stage_state: str | list[str] | None = None,
        priority: int | None = None,
        claimed_by_session_id: str | None = None,
        claimed: bool | None = None,
        closed: bool | None = None,
        escalated: bool | None = None,
        task_type: str | None = None,
        label: str | None = None,
        parent_task_id: str | None = None,
        title_like: str | None = None,
        stages: list[str] | None = None,
        stage_state: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "hierarchy",
        sort_order: str = "asc",
    ) -> list[Task]:
        """List tasks with filtering.

        Args:
            current_stage_state: Filter by current stage state. Can be a single
                state string, a list of states, or None to include all stage states.

        Results are ordered hierarchically: parents appear before their children,
        with siblings sorted by priority ASC, then created_at ASC.
        """
        return _list_tasks(
            self.db,
            project_id=project_id,
            current_stage_state=current_stage_state,
            priority=priority,
            claimed_by_session_id=claimed_by_session_id,
            claimed=claimed,
            closed=closed,
            escalated=escalated,
            task_type=task_type,
            label=label,
            parent_task_id=parent_task_id,
            title_like=title_like,
            stages=stages,
            stage_state=stage_state,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
        )

    def list_epic_guard_scope(self, task_id: str) -> list[Task]:
        """Return the task's ancestors plus its nearest epic ancestor's subtree.

        Cumulative epic guards read only this scope. Serving them from
        `list_tasks` meant paging the whole project and hydrating stage and
        blocking state for every row of it (#20847).
        """
        return _list_epic_guard_scope(self.db, task_id)

    def list_ready_tasks(
        self,
        project_id: str | None = None,
        priority: int | None = None,
        task_type: str | None = None,
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
        current_stage_state: str | list[str] | None = None,
        priority: int | None = None,
        claimed_by_session_id: str | None = None,
        claimed: bool | None = None,
        closed: bool | None = None,
        escalated: bool | None = None,
        task_type: str | None = None,
        label: str | None = None,
        parent_task_id: str | None = None,
        title_like: str | None = None,
        stages: list[str] | None = None,
        stage_state: str | None = None,
    ) -> int:
        """Count tasks with optional filters.

        Args:
            project_id: Filter by project
            current_stage_state: Filter by current stage state

        Returns:
            Count of matching tasks
        """
        return _count_tasks(
            self.db,
            project_id=project_id,
            current_stage_state=current_stage_state,
            priority=priority,
            claimed_by_session_id=claimed_by_session_id,
            claimed=claimed,
            closed=closed,
            escalated=escalated,
            task_type=task_type,
            label=label,
            parent_task_id=parent_task_id,
            title_like=title_like,
            stages=stages,
            stage_state=stage_state,
        )

    def count_by_state(self, project_id: str | None = None) -> dict[str, int]:
        """Count tasks grouped by canonical task state.

        Args:
            project_id: Optional project filter

        Returns:
            Dictionary mapping state to count
        """
        return _count_by_state(self.db, project_id=project_id)

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

    # --- Search Methods ---

    def _ensure_searcher(self) -> TaskSearchBackend:
        """Get or create the task searcher instance."""
        if self._searcher is None:
            self._searcher = TaskSearchBackend(self.db)
        return self._searcher

    def search_tasks(
        self,
        query: str,
        project_id: str | None = None,
        current_stage_state: str | list[str] | None = None,
        task_type: str | None = None,
        priority: int | None = None,
        parent_task_id: str | None = None,
        category: str | None = None,
        limit: int = 20,
        min_score: float = 0.0,
    ) -> list[tuple[Task, float]]:
        """Search tasks using PostgreSQL keyword search.

        Single-query search with SQL filter push-down — all filters
        are applied in the keyword query.

        Args:
            query: Search query text
            project_id: Filter by project
            current_stage_state: Filter by current stage state (string or list of strings)
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
            current_stage_state=current_stage_state,
            task_type=task_type,
            priority=priority,
            parent_task_id=parent_task_id,
            category=category,
            min_score=min_score,
        )

        if not search_results:
            return []

        task_ids = [task_id for task_id, _ in search_results]
        rows = self.db.fetchall("SELECT * FROM tasks WHERE id = ANY(%s)", (task_ids,))
        tasks = [Task.from_row(row) for row in rows]
        hydrate_task_stage_state(self.db, tasks)
        hydrate_task_blocking_state(self.db, tasks)
        task_by_id = {task.id: task for task in tasks}
        return [
            (task_by_id[task_id], score)
            for task_id, score in search_results
            if task_id in task_by_id
        ]

    def reindex_search(self, project_id: str | None = None) -> dict[str, Any]:
        """Return task search index statistics.

        PostgreSQL keyword indexes are maintained by the database extension.

        Args:
            project_id: Unused - kept for API compatibility.

        Returns:
            Dict with index statistics
        """
        searcher = self._ensure_searcher()
        return searcher.reindex()
