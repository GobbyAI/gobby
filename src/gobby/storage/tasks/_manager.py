import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from gobby.storage.database import DatabaseProtocol
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
from gobby.storage.tasks._crud import (
    cascade_build_state_to_subtree as _cascade_build_state_to_subtree,
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
from gobby.storage.tasks._search import TaskFTS5Searcher
from gobby.storage.tasks._stage_registry import StageRegistryManager
from gobby.storage.tasks._stage_states import StageManifestSpec, StageStatesManager
from gobby.storage.tasks._transitions import (
    approve_review as _approve_review,
)
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
    reconcile_task_state as _reconcile_task_state,
)
from gobby.storage.tasks._transitions import (
    reject_review as _reject_review,
)
from gobby.storage.tasks._transitions import (
    release_task_claim as _release_task_claim,
)
from gobby.storage.tasks._transitions import (
    submit_for_review as _submit_for_review,
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


def _stage_cap_value(stage_name: str, field_name: str, value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"stage_caps.{stage_name}.{field_name} must be an integer >= 1")
    return value


def _stage_cap_overrides(
    stage_caps: Sequence[Mapping[str, object]] | None,
) -> dict[str, tuple[int | None, int | None]]:
    overrides: dict[str, tuple[int | None, int | None]] = {}
    for item in stage_caps or ():
        raw_stage_name = item.get("stage_name")
        if not isinstance(raw_stage_name, str) or not raw_stage_name.strip():
            raise ValueError("stage_caps entries require a non-empty stage_name")
        stage_name = raw_stage_name.strip()
        overrides[stage_name] = (
            _stage_cap_value(stage_name, "max_work_attempts", item.get("max_work_attempts")),
            _stage_cap_value(stage_name, "max_review_rounds", item.get("max_review_rounds")),
        )
    return overrides


def _stage_manifest_specs(
    default_stages: Iterable[tuple[str, int]],
    stage_caps: Sequence[Mapping[str, object]] | None,
    stages_override: Sequence[str] | None = None,
) -> list[StageManifestSpec]:
    cap_by_stage = _stage_cap_overrides(stage_caps)
    if stages_override is not None:
        default_stages = [(stage_name, position) for position, stage_name in enumerate(stages_override)]
    specs: list[StageManifestSpec] = []
    seen_names: set[str] = set()
    for stage_name, position in default_stages:
        seen_names.add(stage_name)
        work_cap, review_cap = cap_by_stage.get(stage_name, (None, None))
        specs.append(
            StageManifestSpec(
                stage_name=stage_name,
                position=position,
                max_work_attempts=work_cap,
                max_review_rounds=review_cap,
            )
        )

    unknown_caps = sorted(set(cap_by_stage) - seen_names)
    if unknown_caps:
        raise ValueError(f"stage_caps target stage not in task manifest: {unknown_caps[0]}")
    return specs


class LocalTaskManager:
    def __init__(self, db: DatabaseProtocol):
        self.db = db
        self._change_listeners: list[Callable[[], Any]] = []
        self._searcher: TaskFTS5Searcher | None = None
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
                logger.error(f"Error in task change listener: {e}")

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
        assignee: str | None = None,
        claimed_by_session_id: str | None = None,
        labels: list[str] | None = None,
        category: str | None = None,
        validation_criteria: str | None = None,
        assigned_agent: str | None = None,
        additional_skills: list[str] | None = None,
        github_issue_number: int | None = None,
        github_pr_number: int | None = None,
        github_repo: str | None = None,
        linear_issue_id: str | None = None,
        linear_team_id: str | None = None,
        stage_caps: Sequence[Mapping[str, object]] | None = None,
        stages_override: Sequence[str] | None = None,
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
            labels=labels,
            category=category,
            validation_criteria=validation_criteria,
            assigned_agent=assigned_agent,
            additional_skills=additional_skills,
            github_issue_number=github_issue_number,
            github_pr_number=github_pr_number,
            github_repo=github_repo,
            linear_issue_id=linear_issue_id,
            linear_team_id=linear_team_id,
        )
        if stages_override is not None:
            for stage_name in stages_override:
                if self.stages_registry.get(stage_name) is None:
                    raise ValueError(f"Unknown stage '{stage_name}'")
            default_stages = [(stage_name, position) for position, stage_name in enumerate(stages_override)]
        else:
            default_stages = self.stages_registry.list_default_stages(task_type)
        specs = _stage_manifest_specs(default_stages, stage_caps)
        if specs:
            self.stage_states.initialize_manifest(
                task_id,
                specs,
                by_session_id=created_in_session_id,
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
        additional_skills: MaybeUnset[list[str] | None] = UNSET,
        **kwargs: Any,
    ) -> Task:
        """Update metadata fields only.

        Stage and ownership mutations must go through the dedicated task
        transition methods so claim/session state stays coherent.
        """
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

        parent_changed = _update_task(
            self.db,
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
            additional_skills=additional_skills,
        )

        # If parent_task_id was changed, update path_cache for this task and all descendants
        if parent_changed:
            self.update_descendant_paths(task_id)

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
    ) -> int:
        """Apply build dispatch state to an epic and every descendant task."""
        if unattended is None:
            unattended = bool(yolo)
        updated_count = _cascade_build_state_to_subtree(
            self.db,
            epic_id=epic_id,
            isolation=isolation,
            unattended=unattended,
            allow_automation=allow_automation,
            skip_stages=skip_stages,
        )
        self._notify_listeners()
        return updated_count

    def reconcile_task_state(
        self,
        task_id: str,
        *,
        title: MaybeUnset[str | None] = UNSET,
        description: MaybeUnset[str | None] = UNSET,
        priority: MaybeUnset[int | None] = UNSET,
    ) -> Task:
        """Apply externally-sourced task metadata.

        This is an explicit internal reconciliation path for sync/adaptor code
        that should not use the generic metadata update surface.
        """
        task = _reconcile_task_state(
            self.db,
            task_id=task_id,
            title=title,
            description=description,
            priority=priority,
        )
        self._notify_listeners()
        return task

    def claim_task(self, task_id: str, session_id: str, force: bool = False) -> Task:
        """Claim a task for a session, preserving non-open lifecycle states."""
        task = _claim_task(self.db, task_id=task_id, session_id=session_id, force=force)
        self._notify_listeners()
        return task

    def release_task_claim(
        self,
        task_id: str,
        *,
        description: MaybeUnset[str | None] = UNSET,
        validation_fail_count: MaybeUnset[int | None] = UNSET,
        dispatch_failure_count: MaybeUnset[int | None] = UNSET,
        escalated_at: MaybeUnset[str | None] = UNSET,
        escalation_reason: MaybeUnset[str | None] = UNSET,
    ) -> Task:
        """Clear ownership while optionally changing recovery metadata."""
        task = _release_task_claim(
            self.db,
            task_id=task_id,
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
        """Reopen a task to the ready state.

        Works from any non-ready state. Clears assignee, closed fields,
        and resets validation_fail_count.

        Args:
            task_id: The task ID to reopen
            reason: Optional reason for reopening

        Raises:
            ValueError: If task not found or already ready
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
        """Escalate a task for human intervention and release ownership.

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
        reset_validation: bool = False,
    ) -> Task:
        """Clear escalation state without mutating the task's current stage."""
        task = _de_escalate_task(
            self.db,
            task_id=task_id,
            reason=reason,
            reset_validation=reset_validation,
        )
        self._notify_listeners()
        return task

    def submit_for_review(
        self,
        task_id: str,
        stage_name: str | None = None,
        review_notes: str | None = None,
        *,
        by_session_id: str | None = None,
    ) -> Task:
        """Submit a stage for review and release ownership."""
        task = _submit_for_review(
            self.db,
            task_id=task_id,
            stage_name=stage_name,
            review_notes=review_notes,
            by_session_id=by_session_id,
        )
        self._notify_listeners()
        return task

    def approve_review(
        self,
        task_id: str,
        stage_name: str | None = None,
        approval_notes: str | None = None,
        *,
        by_session_id: str | None = None,
    ) -> Task:
        """Approve review on a stage and release ownership."""
        task = _approve_review(
            self.db,
            task_id=task_id,
            stage_name=stage_name,
            approval_notes=approval_notes,
            by_session_id=by_session_id,
        )
        self._notify_listeners()
        return task

    def reject_review(
        self,
        task_id: str,
        stage_name: str | None = None,
        rejection_notes: str | None = None,
        round_number: int | None = None,
        *,
        by_session_id: str | None = None,
    ) -> Task:
        """Reject review on a stage and return it to ready."""
        task = _reject_review(
            self.db,
            task_id=task_id,
            stage_name=stage_name,
            rejection_notes=rejection_notes,
            round_number=round_number,
            by_session_id=by_session_id,
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
        current_stage_state: str | list[str] | None = None,
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
            sort_by=sort_by,
            sort_order=sort_order,
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
        current_stage_state: str | None = None,
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
        assigned_agent: str | None = None,
        additional_skills: list[str] | None = None,
        stage_caps: Sequence[Mapping[str, object]] | None = None,
        stages_override: Sequence[str] | None = None,
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
            assigned_agent=assigned_agent,
            additional_skills=additional_skills,
            stage_caps=stage_caps,
            stages_override=stages_override,
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
        current_stage_state: str | list[str] | None = None,
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
