from collections.abc import Sequence
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
from gobby.storage.tasks._models import UNSET, MaybeUnset, Task
from gobby.storage.tasks._plan_enhancement import (
    record_plan_enhancement as _record_plan_enhancement,
)
from gobby.storage.tasks._review_transitions import (
    approve_review as _approve_review,
)
from gobby.storage.tasks._review_transitions import (
    reject_review as _reject_review,
)
from gobby.storage.tasks._review_transitions import (
    submit_for_review as _submit_for_review,
)
from gobby.storage.tasks._transitions import (
    claim_task as _claim_task,
)
from gobby.storage.tasks._transitions import (
    escalate_task as _escalate_task,
)
from gobby.storage.tasks._transitions import (
    increment_validation_failure as _increment_validation_failure,
)
from gobby.storage.tasks._transitions import (
    reconcile_task_state as _reconcile_task_state,
)
from gobby.storage.tasks._transitions import (
    release_task_claim as _release_task_claim,
)


class TaskTransitionsMixin:
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
    ) -> Task:
        """Claim a task for a session, preserving non-open lifecycle states."""
        task = _claim_task(
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
        validation_fail_count: MaybeUnset[int | None] = UNSET,
        dispatch_failure_count: MaybeUnset[int | None] = UNSET,
        escalated_at: MaybeUnset[datetime | str | None] = UNSET,
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
        closed_ancestors: list[str] | None = None,
        validation_override_reason: str | None = None,
        expected_updated_at: datetime | None = None,
        reset_validation_fail_count: bool = False,
        validation_status: str | None = None,
        validation_feedback: str | None = None,
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
            validation_override_reason=validation_override_reason,
            expected_updated_at=expected_updated_at,
            reset_validation_fail_count=reset_validation_fail_count,
            validation_status=validation_status,
            validation_feedback=validation_feedback,
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
        validation_override_reason: str | None = None,
        cwd: str | Path | None = None,
    ) -> Task:
        """Link a commit and close the task in one transaction."""
        with self.db.transaction_immediate(TaskLifecycleMutation(task_id=task_id)):
            _link_commit(self.db, task_id, commit_sha, cwd)
            _close_task(
                self.db,
                task_id=task_id,
                reason=reason,
                force=force,
                closed_in_session_id=closed_in_session_id,
                closed_commit_sha=commit_sha,
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

        Works from any non-ready state. Clears ownership, closed fields,
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
        reset_stage_attempts: bool = False,
        restore_stage_from_history: bool = False,
    ) -> Task:
        """Clear escalation state without mutating the task's current stage."""
        task = _de_escalate_task(
            self.db,
            task_id=task_id,
            reason=reason,
            reset_validation=reset_validation,
            reset_stage_attempts=reset_stage_attempts,
            restore_stage_from_history=restore_stage_from_history,
        )
        self._notify_listeners()
        return task

    def increment_validation_failure(
        self,
        task_id: str,
        *,
        expected_updated_at: datetime,
        threshold: int,
        validation_status: str,
        validation_feedback: str | None,
        escalation_reason: str,
    ) -> tuple[int, bool]:
        """Record a guarded validation failure and escalate atomically when due."""
        result = _increment_validation_failure(
            self.db,
            task_id,
            expected_updated_at=expected_updated_at,
            threshold=threshold,
            validation_status=validation_status,
            validation_feedback=validation_feedback,
            escalation_reason=escalation_reason,
        )
        self._notify_listeners()
        return result

    def submit_for_review(
        self,
        task_id: str,
        stage_name: str | None = None,
        review_notes: str | None = None,
        *,
        by_session_id: str | None = None,
        dispatch_run_id: str | None = None,
    ) -> Task:
        """Submit a stage for review and release ownership."""
        task = _submit_for_review(
            self.db,
            task_id=task_id,
            stage_name=stage_name,
            review_notes=review_notes,
            by_session_id=by_session_id,
            dispatch_run_id=dispatch_run_id,
        )
        self._notify_listeners()
        return task

    def approve_review(
        self,
        task_id: str,
        stage_name: str | None = None,
        approval_notes: str | None = None,
        *,
        round_number: int | None = None,
        findings: list[dict[str, object]] | None = None,
        manifest_entries: list[dict[str, object]] | None = None,
        routing_decisions: dict[str, object] | None = None,
        coverage_attestation: dict[str, object] | None = None,
        evidence_id: str | None = None,
        by_session_id: str | None = None,
        dispatch_run_id: str | None = None,
    ) -> Task:
        """Approve review on a stage and release ownership."""
        task = _approve_review(
            self.db,
            task_id=task_id,
            stage_name=stage_name,
            approval_notes=approval_notes,
            round_number=round_number,
            findings=findings,
            manifest_entries=manifest_entries,
            routing_decisions=routing_decisions,
            coverage_attestation=coverage_attestation,
            evidence_id=evidence_id,
            by_session_id=by_session_id,
            dispatch_run_id=dispatch_run_id,
        )
        self._notify_listeners()
        return task

    def reject_review(
        self,
        task_id: str,
        stage_name: str | None = None,
        rejection_notes: str | None = None,
        round_number: int | None = None,
        findings: list[dict[str, object]] | None = None,
        coverage_attestation: dict[str, object] | None = None,
        evidence_id: str | None = None,
        *,
        by_session_id: str | None = None,
        dispatch_run_id: str | None = None,
    ) -> Task:
        """Reject review on a stage and return it to ready."""
        task = _reject_review(
            self.db,
            task_id=task_id,
            stage_name=stage_name,
            rejection_notes=rejection_notes,
            round_number=round_number,
            findings=findings,
            coverage_attestation=coverage_attestation,
            evidence_id=evidence_id,
            by_session_id=by_session_id,
            dispatch_run_id=dispatch_run_id,
        )
        self._notify_listeners()
        return task

    def record_plan_enhancement(
        self,
        task_id: str,
        *,
        round_number: int,
        converged: bool,
        suggestions: Sequence[str] | None = None,
        signoff_summary: str | None = None,
        by_session_id: str | None = None,
    ) -> Task:
        """Record an enhancement round and route the plan back to the planner.

        Suggestions return the planning stage to ready without consuming the
        adversary review budget; convergence leaves it in needs_review.
        """
        task = _record_plan_enhancement(
            self.db,
            task_id,
            round_number=round_number,
            converged=converged,
            suggestions=suggestions,
            signoff_summary=signoff_summary,
            by_session_id=by_session_id,
        )
        self._notify_listeners()
        return task
