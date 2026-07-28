"""Task stage-state manifest storage manager."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from gobby.plans.review_repair import (
    REPAIR_SUBMISSION_ARTIFACT_KEY,
    consumed_repair_submission,
    encode_repair_submission,
)
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._runtime_mutex import RuntimeStageSnapshotState
from gobby.storage.tasks._stage_registry import StageRegistryManager
from gobby.storage.tasks._stage_state_manifest_ops import StageStateManifestOps
from gobby.storage.tasks._stage_state_mutex import StageStateMutexFactory, snapshot_state
from gobby.storage.tasks._stage_state_rows import StageStateRows
from gobby.storage.tasks._stage_state_schema import StageStateSchema
from gobby.storage.tasks._stage_state_transitions import (
    StageStateTransitions,
    illegal,
    terminal_after_done,
)
from gobby.storage.tasks._stage_types import (
    IllegalManifestMutationError,
    IllegalStageTransitionError,
    ManifestAlreadyInitializedError,
    ManifestMutation,
    ManifestMutationReason,
    NoCurrentStageError,
    StageManifestSpec,
    StageState,
    StageState5,
)
from gobby.storage.tasks._stage_utils import _close_task_in_txn

__all__ = [
    "IllegalManifestMutationError",
    "IllegalStageTransitionError",
    "ManifestAlreadyInitializedError",
    "ManifestMutation",
    "ManifestMutationReason",
    "NoCurrentStageError",
    "StageManifestSpec",
    "StageState",
    "StageState5",
    "StageStatesManager",
    "_close_task_in_txn",
]


class StageStatesManager:
    def __init__(self, db: HubDatabase, events: TaskLifecycleEventManager) -> None:
        self.db = db
        self.events = events
        self.registry = StageRegistryManager(db)
        self.mutexes = TaskDispatchMutexManager(db)
        self._rows = StageStateRows(db, self.registry)
        self._mutexes = StageStateMutexFactory(self.mutexes, self._rows)
        self._schema = StageStateSchema(db, self._rows)
        self._manifest = StageStateManifestOps(db, events, self._rows, self._mutexes)
        self._transitions = StageStateTransitions(db, events, self._rows, self._mutexes)

    def list_for_task(self, task_id: str) -> list[StageState]:
        return self._rows.list_for_task(task_id)

    def get(self, task_id: str, stage_name: str) -> StageState | None:
        return self._rows.get(task_id, stage_name)

    def current_stage(self, task_id: str) -> StageState | None:
        return self._rows.current_stage(task_id)

    def list_tasks_at_stage(
        self,
        *,
        stage_name: str,
        state: str | None = None,
        project_id: str | None = None,
    ) -> list[str]:
        return self._rows.list_tasks_at_stage(
            stage_name=stage_name,
            state=state,
            project_id=project_id,
        )

    def initialize_manifest(
        self,
        task_id: str,
        specs: Sequence[StageManifestSpec],
        *,
        by_session_id: str | None,
        replace_existing: bool = False,
    ) -> list[StageState]:
        return self._manifest.initialize_manifest(
            task_id,
            specs,
            by_session_id=by_session_id,
            replace_existing=replace_existing,
        )

    def insert_new_task_manifest_in_transaction(
        self,
        conn: Transaction,
        task_id: str,
        specs: Sequence[StageManifestSpec],
        *,
        by_session_id: str | None,
    ) -> list[StageState]:
        """Insert initial stage rows using the caller's existing task-create transaction."""
        return self._manifest.insert_new_task_manifest_in_transaction(
            conn,
            task_id,
            specs,
            by_session_id=by_session_id,
        )

    def replace_manifest(
        self,
        task_id: str,
        specs: Sequence[StageManifestSpec],
        *,
        expected_existing_shape: Sequence[tuple[str, int, int | None, int | None]],
        from_state: str,
        reason: str,
        by_session_id: str | None,
        by_actor: str,
    ) -> list[StageState] | None:
        return self._manifest.replace_manifest(
            task_id,
            specs,
            expected_existing_shape=expected_existing_shape,
            from_state=from_state,
            reason=reason,
            by_session_id=by_session_id,
            by_actor=by_actor,
        )

    def add_stage(
        self,
        task_id: str,
        spec: StageManifestSpec,
        *,
        by_session_id: str | None,
    ) -> StageState:
        return self._manifest.add_stage(task_id, spec, by_session_id=by_session_id)

    def remove_stage(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
    ) -> None:
        self._manifest.remove_stage(task_id, stage_name, by_session_id=by_session_id)

    def start_stage(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
        notes: str | None = None,
        dispatch_run_id: str | None = None,
    ) -> StageState:
        return self._transition(
            task_id,
            stage_name,
            "start_stage",
            by_session_id=by_session_id,
            notes=notes,
            dispatch_run_id=dispatch_run_id,
        )

    def submit_for_review(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
        notes: str | None = None,
        repair_submission: Mapping[str, object] | None = None,
        dispatch_run_id: str | None = None,
        preheld_mutex_run_id: str | None = None,
    ) -> StageState:
        artifact_updates = (
            {REPAIR_SUBMISSION_ARTIFACT_KEY: encode_repair_submission(repair_submission)}
            if repair_submission is not None
            else None
        )
        return self._transition(
            task_id,
            stage_name,
            "submit_for_review",
            by_session_id=by_session_id,
            notes=notes,
            artifact_updates=artifact_updates,
            dispatch_run_id=dispatch_run_id,
            preheld_mutex_run_id=preheld_mutex_run_id,
        )

    def consume_plan_review_submission(
        self,
        task_id: str,
        stage_name: str,
        *,
        raw_submission: str,
        evidence_id: str,
    ) -> StageState:
        """Persist an idempotent receipt after evidence preparation consumes a payload."""
        receipt = consumed_repair_submission(raw_submission, evidence_id=evidence_id)
        with self.db.transaction() as transaction:
            row = transaction.execute(
                """
                UPDATE task_stage_states
                   SET artifact_refs = jsonb_set(
                           COALESCE(artifact_refs, '{}'::jsonb),
                           ARRAY[%s],
                           to_jsonb(%s::text),
                           true
                       ),
                       updated_at = NOW()
                 WHERE task_id = %s
                   AND stage_name = %s
                   AND artifact_refs ->> %s = %s
                RETURNING *
                """,
                (
                    REPAIR_SUBMISSION_ARTIFACT_KEY,
                    receipt,
                    task_id,
                    stage_name,
                    REPAIR_SUBMISSION_ARTIFACT_KEY,
                    raw_submission,
                ),
            ).fetchone()
        if row is not None:
            return self._state_from_row(row)
        current = self.get(task_id, stage_name)
        current_submission = (
            (current.artifact_refs or {}).get(REPAIR_SUBMISSION_ARTIFACT_KEY)
            if current is not None
            else None
        )
        if current is not None and current_submission == receipt:
            return current
        raise ValueError("plan review repair submission changed before consumption")

    def approve_review(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
        notes: str | None = None,
        dispatch_run_id: str | None = None,
    ) -> StageState:
        return self._transition(
            task_id,
            stage_name,
            "approve_review",
            by_session_id=by_session_id,
            notes=notes,
            dispatch_run_id=dispatch_run_id,
        )

    def reject_review(
        self,
        task_id: str,
        stage_name: str,
        *,
        reason: str,
        by_session_id: str | None,
        notes: str | None = None,
        dispatch_run_id: str | None = None,
    ) -> StageState:
        return self._transition(
            task_id,
            stage_name,
            "reject_review",
            by_session_id=by_session_id,
            notes=notes,
            reason=reason,
            dispatch_run_id=dispatch_run_id,
        )

    def route_enhancement(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
        notes: str | None = None,
        _transaction_mutation: Callable[[Transaction], None] | None = None,
    ) -> StageState:
        """Return a needs_review stage to ready for an enhancement re-plan.

        Unlike ``reject_review`` this does not increment ``review_round_count``;
        enhancement rounds are tracked independently in task artifacts.
        """
        return self._transition(
            task_id,
            stage_name,
            "route_enhancement",
            by_session_id=by_session_id,
            notes=notes,
            _transaction_mutation=_transaction_mutation,
        )

    def complete_stage(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
        commit_sha: str | None = None,
        artifact_updates: Mapping[str, str] | None = None,
        validation_override_reason: str | None = None,
        dispatch_run_id: str | None = None,
        preheld_mutex_run_id: str | None = None,
    ) -> StageState:
        return self._transition(
            task_id,
            stage_name,
            "complete_stage",
            by_session_id=by_session_id,
            commit_sha=commit_sha,
            artifact_updates=artifact_updates,
            validation_override_reason=validation_override_reason,
            dispatch_run_id=dispatch_run_id,
            preheld_mutex_run_id=preheld_mutex_run_id,
        )

    def fail_stage(
        self,
        task_id: str,
        stage_name: str,
        *,
        reason: str,
        needs_human: bool = False,
        by_session_id: str | None,
        cited_subtasks: Sequence[str] | None = None,
        dispatch_run_id: str | None = None,
        preheld_mutex_run_id: str | None = None,
    ) -> StageState:
        return self._transition(
            task_id,
            stage_name,
            "fail_stage",
            needs_human=needs_human,
            by_session_id=by_session_id,
            reason=reason,
            cited_subtasks=cited_subtasks,
            dispatch_run_id=dispatch_run_id,
            preheld_mutex_run_id=preheld_mutex_run_id,
        )

    def recover_abandoned_stage(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
        reason: str = "abandoned_in_progress_recovered",
        preheld_mutex_run_id: str | None = None,
    ) -> StageState:
        return self._transitions.recover_abandoned_stage(
            task_id,
            stage_name,
            by_session_id=by_session_id,
            reason=reason,
            preheld_mutex_run_id=preheld_mutex_run_id,
        )

    def move_to_stage(
        self,
        task_id: str,
        target_stage_name: str,
        *,
        by_session_id: str | None,
        notes: str | None = None,
        force: bool = False,
    ) -> StageState:
        """Move a task to the target stage, resetting stage rows as needed."""
        return self._transitions.move_task_to_stage(
            task_id,
            target_stage_name,
            by_session_id=by_session_id,
            notes=notes,
            force=force,
        )

    def escalate_stage_failure(self, task_id: str, reason: str) -> None:
        self._transitions.escalate_stage_failure(task_id, reason)

    def _transition(
        self,
        task_id: str,
        stage_name: str,
        verb: str,
        *,
        by_session_id: str | None,
        notes: str | None = None,
        reason: str | None = None,
        needs_human: bool = False,
        commit_sha: str | None = None,
        artifact_updates: Mapping[str, str] | None = None,
        validation_override_reason: str | None = None,
        cited_subtasks: Sequence[str] | None = None,
        dispatch_run_id: str | None = None,
        preheld_mutex_run_id: str | None = None,
        _transaction_mutation: Callable[[Transaction], None] | None = None,
    ) -> StageState:
        return self._transitions.transition(
            task_id,
            stage_name,
            verb,
            by_session_id=by_session_id,
            notes=notes,
            reason=reason,
            needs_human=needs_human,
            commit_sha=commit_sha,
            artifact_updates=artifact_updates,
            validation_override_reason=validation_override_reason,
            cited_subtasks=cited_subtasks,
            dispatch_run_id=dispatch_run_id,
            preheld_mutex_run_id=preheld_mutex_run_id,
            _transaction_mutation=_transaction_mutation,
        )

    def _state_from_row(self, row: Mapping[str, Any]) -> StageState:
        return self._rows.state_from_row(row)

    def _ensure_phase2_columns(self) -> None:
        self._schema.ensure_phase2_columns()

    @staticmethod
    def _snapshot_state(value: StageState5) -> RuntimeStageSnapshotState | None:
        return snapshot_state(value)

    @staticmethod
    def _illegal(row: StageState, verb: str) -> IllegalStageTransitionError:
        return illegal(row, verb)

    @staticmethod
    def _terminal_after_done(
        conn: Transaction,
        task_id: str,
        stage_name: str,
    ) -> bool:
        return terminal_after_done(conn, task_id, stage_name)

    def _escalate(self, task_id: str, reason: str) -> None:
        self.escalate_stage_failure(task_id, reason)
