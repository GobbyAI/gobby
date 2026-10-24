"""Task metadata-update orchestration on the task manager's shared database."""

from datetime import datetime
from typing import Any, cast

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.storage.tasks._close import close_eligible_parent_chain
from gobby.storage.tasks._models import UNSET, MaybeUnset, Task
from gobby.storage.tasks._updates import update_task_metadata as _update_task_metadata
from gobby.tasks.criteria_text import require_validation_criteria
from gobby.utils.datetime import utc_now


class TaskUpdateMixin:
    db: HubDatabase

    def get_task(self, task_id: str, project_id: str | None = None) -> Task:
        raise NotImplementedError

    def update_descendant_paths(self, task_id: str) -> int:
        raise NotImplementedError

    def _notify_listeners(self) -> None:
        raise NotImplementedError

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
        review_status: MaybeUnset[str | None] = UNSET,
        review_feedback: MaybeUnset[str | None] = UNSET,
        category: MaybeUnset[str | None] = UNSET,
        validation_criteria: MaybeUnset[str | None] = UNSET,
        review_fail_count: MaybeUnset[int | None] = UNSET,
        merge_in_progress: MaybeUnset[bool] = UNSET,
        blocked_by_merge: MaybeUnset[bool] = UNSET,
        escalated_at: MaybeUnset[datetime | str | None] = UNSET,
        escalation_reason: MaybeUnset[str | None] = UNSET,
        validation_override_reason: MaybeUnset[str | None] = UNSET,
        assigned_agent: MaybeUnset[str | None] = UNSET,
        implementation_domain: MaybeUnset[str | None] = UNSET,
        additional_skills: MaybeUnset[list[str] | None] = UNSET,
        start_date: MaybeUnset[str | None] = UNSET,
        due_date: MaybeUnset[str | None] = UNSET,
        affected_files: MaybeUnset[list[str]] = UNSET,
        **kwargs: Any,
    ) -> Task:
        """Update metadata fields and optionally replace declared file scope.

        Task/session ownership mutations must go through the dedicated task
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

        vacated_parent_id: str | None = None
        with self.db.transaction() as conn:
            moved_from = (
                None
                if parent_task_id is UNSET
                else conn.execute(
                    "SELECT parent_task_id, closed_at FROM tasks WHERE id = %s FOR UPDATE",
                    (task_id,),
                ).fetchone()
            )
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
                review_status=review_status,
                review_feedback=review_feedback,
                category=category,
                validation_criteria=validation_criteria,
                review_fail_count=review_fail_count,
                merge_in_progress=merge_in_progress,
                blocked_by_merge=blocked_by_merge,
                escalated_at=escalated_at,
                escalation_reason=escalation_reason,
                validation_override_reason=validation_override_reason,
                assigned_agent=assigned_agent,
                implementation_domain=implementation_domain,
                additional_skills=additional_skills,
                start_date=start_date,
                due_date=due_date,
                **kwargs,
            )

            if parent_changed:
                self.update_descendant_paths(task_id)
                if (
                    moved_from is not None
                    and moved_from["closed_at"] is None
                    and moved_from["parent_task_id"] != parent_task_id
                ):
                    vacated_parent_id = moved_from["parent_task_id"]

            if affected_files is not UNSET:
                TaskAffectedFileManager(self.db).replace_declared_files(
                    task_id,
                    cast(list[str], affected_files),
                )

        if vacated_parent_id is not None:
            # Moving an open child out can leave the old parent with none, and no
            # child close will ever arrive to close it (#23384). The walk runs after
            # the move commits: inside it, the walk waited on the old parent row while
            # holding the proposed parent row, and an inverse move deadlocked on them.
            with self.db.transaction() as conn:
                close_eligible_parent_chain(
                    conn,
                    vacated_parent_id,
                    db=self.db,
                    reason=None,
                    closed_at=utc_now(),
                    closed_in_session_id=None,
                    closed_ancestors=None,
                )

        self._notify_listeners()
        return self.get_task(task_id)
