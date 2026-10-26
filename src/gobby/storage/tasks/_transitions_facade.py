from datetime import datetime
from pathlib import Path

from gobby.storage.hub.protocol import HubDatabase, TaskLifecycleMutation
from gobby.storage.tasks._de_escalation import (
    de_escalate_task as _de_escalate_task,
)
from gobby.storage.tasks._lifecycle import (
    close_task as _close_task,
)
from gobby.storage.tasks._lifecycle import (
    link_commit as _link_commit,
)
from gobby.storage.tasks._lifecycle import (
    reopen_task as _reopen_task,
)
from gobby.storage.tasks._lifecycle import repair_closed_candidate as _repair_closed_candidate
from gobby.storage.tasks._models import UNSET, MaybeUnset, Task, TaskStaleStateError
from gobby.storage.tasks._transitions import (
    claim_task as _claim_task,
)
from gobby.storage.tasks._transitions import (
    claim_task_for_agent as _claim_task_for_agent,
)
from gobby.storage.tasks._transitions import (
    escalate_task as _escalate_task,
)
from gobby.storage.tasks._transitions import (
    reconcile_task_state as _reconcile_task_state,
)
from gobby.storage.tasks._transitions import (
    release_escalated_task_claim as _release_escalated_task_claim,
)
from gobby.storage.tasks._transitions import (
    release_task_claim as _release_task_claim,
)


class TaskTransitionsMixin:
    def repair_closed_candidate(
        self,
        task_id: str,
        *,
        review_id: str,
        candidate_commit_sha: str,
        expected_closed_commit_sha: str,
        by_session_id: str,
        reason: str,
        preview: bool = True,
    ) -> dict[str, object]:
        return _repair_closed_candidate(
            self.db,
            task_id,
            review_id=review_id,
            candidate_commit_sha=candidate_commit_sha,
            expected_closed_commit_sha=expected_closed_commit_sha,
            by_session_id=by_session_id,
            reason=reason,
            preview=preview,
        )

    db: HubDatabase

    def _notify_listeners(self) -> None:
        raise NotImplementedError

    def get_task(self, task_id: str, project_id: str | None = None) -> Task:
        raise NotImplementedError

    def reconcile_task_state(
        self,
        task_id: str,
        *,
        title: MaybeUnset[str | None] = UNSET,
        description: MaybeUnset[str | None] = UNSET,
        priority: MaybeUnset[int | None] = UNSET,
        closed_reason: MaybeUnset[str | None] = UNSET,
        closed_at: MaybeUnset[str | None] = UNSET,
        closed_in_session_id: MaybeUnset[str | None] = UNSET,
        closed_commit_sha: MaybeUnset[str | None] = UNSET,
        escalated_at: MaybeUnset[datetime | str | None] = UNSET,
        escalation_reason: MaybeUnset[str | None] = UNSET,
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
            closed_reason=closed_reason,
            closed_at=closed_at,
            closed_in_session_id=closed_in_session_id,
            closed_commit_sha=closed_commit_sha,
            escalated_at=escalated_at,
            escalation_reason=escalation_reason,
        )
        self._notify_listeners()
        return task

    def claim_task(
        self,
        task_id: str,
        session_id: str,
        force: bool = False,
        *,
        expected_owner: str | None = None,
        select_claim: bool = True,
    ) -> Task:
        """Claim a task for a session, preserving non-open lifecycle states."""
        task = _claim_task(
            self.db,
            task_id=task_id,
            session_id=session_id,
            force=force,
            expected_owner=expected_owner,
            select_claim=select_claim,
        )
        self._notify_listeners()
        return task

    def claim_task_for_agent(
        self,
        task_id: str,
        session_id: str,
        force: bool = False,
        *,
        expected_owner: str | None = None,
    ) -> Task:
        """Serialize an agent claim and select its task atomically."""
        task = _claim_task_for_agent(
            self.db,
            task_id=task_id,
            session_id=session_id,
            force=force,
            expected_owner=expected_owner,
        )
        self._notify_listeners()
        return task

    def release_task_claim(
        self,
        task_id: str,
        *,
        description: MaybeUnset[str | None] = UNSET,
        review_fail_count: MaybeUnset[int | None] = UNSET,
        escalated_at: MaybeUnset[datetime | str | None] = UNSET,
        escalation_reason: MaybeUnset[str | None] = UNSET,
    ) -> Task:
        """Clear ownership while optionally changing recovery metadata."""
        task = _release_task_claim(
            self.db,
            task_id=task_id,
            description=description,
            review_fail_count=review_fail_count,
            escalated_at=escalated_at,
            escalation_reason=escalation_reason,
        )
        self._notify_listeners()
        return task

    def release_escalated_task_claim(self, task_id: str, *, expected_owner: str) -> Task | None:
        """Clear the owner's claim on an escalated task, keeping its escalation metadata."""
        task = _release_escalated_task_claim(self.db, task_id, expected_owner=expected_owner)
        if task is not None:
            self._notify_listeners()
        return task

    def close_task(
        self,
        task_id: str,
        reason: str | None = None,
        force: bool = False,
        closed_in_session_id: str | None = None,
        closed_commit_sha: str | None = None,
        closed_ancestors: list[str] | None = None,
        expected_updated_at: datetime | None = None,
        closing_note: str | None = None,
    ) -> Task:
        """Close a task."""
        _close_task(
            self.db,
            task_id=task_id,
            reason=reason,
            force=force,
            closed_in_session_id=closed_in_session_id,
            closed_commit_sha=closed_commit_sha,
            closed_ancestors=closed_ancestors,
            expected_updated_at=expected_updated_at,
            closing_note=closing_note,
        )
        self._notify_listeners()
        return self.get_task(task_id)

    def close_task_with_commit(
        self,
        task_id: str,
        commit_sha: str,
        *,
        reason: str | None = None,
        force: bool = False,
        closed_in_session_id: str | None = None,
        cwd: str | Path | None = None,
        expected_updated_at: datetime | None = None,
        closed_ancestors: list[str] | None = None,
        closing_note: str | None = None,
    ) -> Task:
        """Link a commit and close the task in one transaction.

        The link stores Git's canonical short SHA, like every other commit writer (#23251).
        """
        from gobby.utils.git import normalize_commit_sha

        short_sha = normalize_commit_sha(commit_sha, cwd=cwd)
        if short_sha is None:
            raise ValueError(f"Invalid or unresolved commit SHA: {commit_sha}")
        with self.db.transaction_immediate(TaskLifecycleMutation(task_id=task_id)) as conn:
            row = conn.execute(
                "SELECT closed_at, updated_at FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Task {task_id} not found")
            if row["closed_at"] is not None:
                return self.get_task(task_id)
            if expected_updated_at is not None and row["updated_at"] != expected_updated_at:
                raise TaskStaleStateError(task_id)
            _link_commit(self.db, task_id, short_sha)
            _close_task(
                self.db,
                task_id=task_id,
                reason=reason,
                force=force,
                closed_in_session_id=closed_in_session_id,
                closed_commit_sha=commit_sha,
                closed_ancestors=closed_ancestors,
                closing_note=closing_note,
            )
        self._notify_listeners()
        return self.get_task(task_id)

    def reopen_task(
        self,
        task_id: str,
        reason: str | None = None,
    ) -> Task:
        """Reopen a task to the ready state.

        Works from any non-ready state. Clears ownership, closed fields,
        and closure metadata.

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
    ) -> Task:
        """Clear escalation state while preserving historical validation records."""
        task = _de_escalate_task(
            self.db,
            task_id=task_id,
            reason=reason,
        )
        self._notify_listeners()
        return task
