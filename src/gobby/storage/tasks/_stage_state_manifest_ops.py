"""Manifest initialization and mutation for task stage-state rows."""

from __future__ import annotations

from collections.abc import Sequence

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._stage_reviewer_selector import resolve_stage_reviewer
from gobby.storage.tasks._stage_state_mutex import StageStateMutexFactory
from gobby.storage.tasks._stage_state_rows import StageStateRows, shape_signature_for_specs
from gobby.storage.tasks._stage_types import (
    IllegalManifestMutationError,
    ManifestAlreadyInitializedError,
    ManifestMutationReason,
    StageManifestSpec,
    StageState,
)
from gobby.storage.tasks._stage_utils import _now


class StageStateManifestOps:
    def __init__(
        self,
        db: HubDatabase,
        events: TaskLifecycleEventManager,
        rows: StageStateRows,
        mutexes: StageStateMutexFactory,
    ) -> None:
        self.db = db
        self.events = events
        self.rows = rows
        self.mutexes = mutexes

    def initialize_manifest(
        self,
        task_id: str,
        specs: Sequence[StageManifestSpec],
        *,
        by_session_id: str | None,
    ) -> list[StageState]:
        self.rows.validate_specs(specs)
        holder = by_session_id or "system"
        snapshot = self.rows.current_stage(task_id)
        with self.mutexes.mutex(task_id, holder, "initialize_manifest", expected_stage=snapshot):
            existing = self.rows.list_for_task(task_id)
            if existing:
                if [
                    (row.stage_name, row.position, row.max_work_attempts, row.max_review_rounds)
                    for row in existing
                ] == [
                    (
                        spec.stage_name,
                        spec.position,
                        spec.max_work_attempts,
                        spec.max_review_rounds,
                    )
                    for spec in sorted(specs, key=lambda item: item.position)
                ]:
                    return existing
                if not all(
                    row.state == "ready"
                    and row.entered_at is None
                    and row.completed_at is None
                    and row.work_attempt_count == 0
                    and row.review_round_count == 0
                    and row.artifact_refs is None
                    and row.notes is None
                    for row in existing
                ):
                    raise ManifestAlreadyInitializedError(task_id)

            now = _now()
            with self.db.transaction() as conn:
                if existing:
                    conn.execute(
                        "DELETE FROM task_stage_states WHERE task_id = ?",
                        (task_id,),
                    )
                for spec in sorted(specs, key=lambda item: item.position):
                    registry = self.rows.registry_entry(spec.stage_name)
                    conn.execute(
                        """
                        INSERT INTO task_stage_states (
                            task_id, stage_name, position, state, review_policy,
                            reviewer_agent, work_attempt_count, review_round_count,
                            max_work_attempts, max_review_rounds, updated_at
                        )
                        VALUES (?, ?, ?, 'ready', ?, ?, 0, 0, ?, ?, ?)
                        """,
                        (
                            task_id,
                            spec.stage_name,
                            spec.position,
                            registry.review_policy,
                            resolve_stage_reviewer(self.db, task_id, registry),
                            spec.max_work_attempts,
                            spec.max_review_rounds,
                            now,
                        ),
                    )
                self.events.record_lifecycle_event(
                    task_id,
                    None,
                    f"manifest:{shape_signature_for_specs(specs)}",
                    "initialize_manifest",
                    by_actor=holder,
                )
            return self.rows.list_for_task(task_id)

    def add_stage(
        self,
        task_id: str,
        spec: StageManifestSpec,
        *,
        by_session_id: str | None,
    ) -> StageState:
        holder = by_session_id or "system"
        snapshot = self.rows.current_stage(task_id)
        with self.mutexes.mutex(
            task_id,
            holder,
            f"{spec.stage_name}:add_stage",
            expected_stage=snapshot,
        ):
            current = self.rows.current_stage(task_id)
            self.validate_add(task_id, spec, current)
            registry = self.rows.registry_entry(spec.stage_name)
            previous_shape = self.rows.shape_signature(task_id)
            now = _now()
            with self.db.transaction() as conn:
                rows = conn.execute(
                    """
                    SELECT stage_name, position
                      FROM task_stage_states
                     WHERE task_id = ? AND position >= ?
                     ORDER BY position DESC
                    """,
                    (task_id, spec.position),
                ).fetchall()
                for row in rows:
                    conn.execute(
                        """
                        UPDATE task_stage_states
                           SET position = ?, updated_at = ?
                         WHERE task_id = ? AND stage_name = ?
                        """,
                        (int(row["position"]) + 1, now, task_id, row["stage_name"]),
                    )
                conn.execute(
                    """
                    INSERT INTO task_stage_states (
                        task_id, stage_name, position, state, review_policy,
                        reviewer_agent, work_attempt_count, review_round_count,
                        max_work_attempts, max_review_rounds, updated_at
                    )
                    VALUES (?, ?, ?, 'ready', ?, ?, 0, 0, ?, ?, ?)
                    """,
                    (
                        task_id,
                        spec.stage_name,
                        spec.position,
                        registry.review_policy,
                        resolve_stage_reviewer(self.db, task_id, registry),
                        spec.max_work_attempts,
                        spec.max_review_rounds,
                        now,
                    ),
                )
                self.record_shape_event(task_id, previous_shape, "add_stage", holder)
            added = self.rows.get(task_id, spec.stage_name)
            if added is None:
                raise RuntimeError(f"Stage '{spec.stage_name}' disappeared after add_stage")
            return added

    def remove_stage(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
    ) -> None:
        holder = by_session_id or "system"
        snapshot = self.rows.current_stage(task_id)
        with self.mutexes.mutex(
            task_id,
            holder,
            f"{stage_name}:remove_stage",
            expected_stage=snapshot,
        ):
            row = self.rows.get(task_id, stage_name)
            current = self.rows.current_stage(task_id)
            self.validate_remove(task_id, stage_name, row, current)
            previous_shape = self.rows.shape_signature(task_id)
            now = _now()
            if row is None:
                raise RuntimeError(f"Stage '{stage_name}' disappeared before remove_stage")
            with self.db.transaction() as conn:
                conn.execute(
                    "DELETE FROM task_stage_states WHERE task_id = ? AND stage_name = ?",
                    (task_id, stage_name),
                )
                rows = conn.execute(
                    """
                    SELECT stage_name, position
                      FROM task_stage_states
                     WHERE task_id = ? AND position > ?
                     ORDER BY position
                    """,
                    (task_id, row.position),
                ).fetchall()
                for existing in rows:
                    conn.execute(
                        """
                        UPDATE task_stage_states
                           SET position = ?, updated_at = ?
                         WHERE task_id = ? AND stage_name = ?
                        """,
                        (
                            int(existing["position"]) - 1,
                            now,
                            task_id,
                            existing["stage_name"],
                        ),
                    )
                self.record_shape_event(task_id, previous_shape, "remove_stage", holder)

    def record_shape_event(
        self,
        task_id: str,
        previous_shape: str,
        reason: str,
        holder: str,
    ) -> None:
        self.events.record_lifecycle_event(
            task_id,
            f"manifest:{previous_shape}",
            f"manifest:{self.rows.shape_signature(task_id)}",
            reason,
            by_actor=holder,
        )

    def validate_add(
        self,
        task_id: str,
        spec: StageManifestSpec,
        current: StageState | None,
    ) -> None:
        if current is None:
            raise IllegalManifestMutationError(
                task_id,
                spec.stage_name,
                spec.position,
                None,
                None,
                "add_stage",
                "manifest_exhausted",
            )
        if self.rows.get(task_id, spec.stage_name) is not None:
            raise IllegalManifestMutationError(
                task_id,
                spec.stage_name,
                spec.position,
                current.stage_name,
                current.state,
                "add_stage",
                "stage_already_in_manifest",
            )
        if spec.position <= current.position:
            raise IllegalManifestMutationError(
                task_id,
                spec.stage_name,
                spec.position,
                current.stage_name,
                current.state,
                "add_stage",
                "position_at_or_before_current",
            )

    def validate_remove(
        self,
        task_id: str,
        stage_name: str,
        row: StageState | None,
        current: StageState | None,
    ) -> None:
        if current is None:
            raise IllegalManifestMutationError(
                task_id, stage_name, None, None, None, "remove_stage", "manifest_exhausted"
            )
        if row is None:
            raise IllegalManifestMutationError(
                task_id,
                stage_name,
                None,
                current.stage_name,
                current.state,
                "remove_stage",
                "stage_not_in_manifest",
            )
        if row.position <= current.position:
            raise IllegalManifestMutationError(
                task_id,
                stage_name,
                row.position,
                current.stage_name,
                current.state,
                "remove_stage",
                "position_at_or_before_current",
            )
        if row.state == "done":
            reason: ManifestMutationReason = "done_row_not_removable"
        elif row.state != "ready":
            reason = "current_row_not_removable"
        else:
            remaining_future = [
                item
                for item in self.rows.list_for_task(task_id)
                if item.stage_name != stage_name and item.position > current.position
            ]
            if not remaining_future:
                reason = "would_exhaust_terminal_position"
            else:
                return
        raise IllegalManifestMutationError(
            task_id,
            stage_name,
            row.position,
            current.stage_name,
            current.state,
            "remove_stage",
            reason,
        )
