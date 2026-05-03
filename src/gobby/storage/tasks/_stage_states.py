"""Task stage-state manifest storage manager."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from gobby.plans.bootstrap_ledger import bootstrap_ledger_path_for_task, verify_bootstrap_ledger
from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._runtime_mutex import RuntimeDispatchMutex, RuntimeStageSnapshotState
from gobby.storage.tasks._stage_registry import (
    ReviewPolicy,
    StageRegistryEntry,
    StageRegistryManager,
)

logger = logging.getLogger(__name__)

StageState5 = Literal["ready", "in_progress", "needs_review", "review_approved", "done"]
ManifestMutation = Literal["add_stage", "remove_stage"]
ManifestMutationReason = Literal[
    "position_at_or_before_current",
    "current_row_not_removable",
    "done_row_not_removable",
    "would_exhaust_terminal_position",
    "stage_already_in_manifest",
    "stage_not_in_manifest",
    "manifest_exhausted",
]


class IllegalStageTransitionError(ValueError):
    """Raised when a stage transition is rejected by policy or source state."""

    def __init__(
        self,
        stage_name: str,
        current_state: StageState5,
        attempted_transition: str,
        review_policy: ReviewPolicy,
    ) -> None:
        self.stage_name = stage_name
        self.current_state = current_state
        self.attempted_transition = attempted_transition
        self.review_policy = review_policy
        super().__init__(stage_name, current_state, attempted_transition, review_policy)


class NoCurrentStageError(ValueError):
    """Raised when a task manifest has no active stage row."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(task_id)


class IllegalManifestMutationError(ValueError):
    """Raised when a structural manifest mutation is rejected."""

    def __init__(
        self,
        task_id: str,
        target_stage_name: str,
        target_position: int | None,
        current_stage_name: str | None,
        current_stage_state: StageState5 | None,
        mutation: ManifestMutation,
        reason: ManifestMutationReason,
    ) -> None:
        self.task_id = task_id
        self.target_stage_name = target_stage_name
        self.target_position = target_position
        self.current_stage_name = current_stage_name
        self.current_stage_state = current_stage_state
        self.mutation = mutation
        self.reason = reason
        super().__init__(
            task_id,
            target_stage_name,
            target_position,
            current_stage_name,
            current_stage_state,
            mutation,
            reason,
        )


class ManifestAlreadyInitializedError(ValueError):
    """Raised when a task already has a different stage manifest."""


@dataclass(frozen=True, slots=True)
class StageState:
    task_id: str
    stage_name: str
    position: int
    state: StageState5
    review_policy: ReviewPolicy
    reviewer_agent: str | None
    entered_at: str | None
    entered_by_session_id: str | None
    completed_at: str | None
    completed_by_session_id: str | None
    completed_commit_sha: str | None
    work_attempt_count: int
    review_round_count: int
    max_work_attempts: int | None
    max_review_rounds: int | None
    artifact_refs: dict[str, str] | None
    notes: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class StageManifestSpec:
    stage_name: str
    position: int
    max_work_attempts: int | None = None
    max_review_rounds: int | None = None

    @classmethod
    def from_position_tuple(cls, value: tuple[str, int]) -> StageManifestSpec:
        return cls(stage_name=value[0], position=value[1])


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _coerce_artifact_refs(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        return None
    return {str(key): str(item) for key, item in decoded.items()}


def _close_task_in_txn(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    db: DatabaseProtocol | None = None,
    reason: str | None = None,
    commit_sha: str | None = None,
    closed_at: str | None = None,
    closed_in_session_id: str | None = None,
    force: bool = False,
    cascade_descendants: bool = False,
    validation_override_reason: str | None = None,
) -> None:
    """Close a task inside the caller's already-open transaction."""

    if not force and not cascade_descendants:
        open_children = conn.execute(
            "SELECT id, title FROM tasks WHERE parent_task_id = ? AND closed_at IS NULL",
            (task_id,),
        ).fetchall()
        if open_children:
            child_list = ", ".join(
                f"{child['id']} ({child['title']})" for child in open_children[:3]
            )
            if len(open_children) > 3:
                child_list += f" and {len(open_children) - 3} more"
            raise ValueError(
                f"Cannot close task {task_id}: has {len(open_children)} open child task(s): "
                f"{child_list}"
            )

    if db is not None and bootstrap_ledger_path_for_task(db, task_id) is not None:
        verify_bootstrap_ledger(db, task_id)

    now = closed_at or _now()
    persisted_session_id = (
        closed_in_session_id if _session_exists(conn, closed_in_session_id) else None
    )
    conn.execute(
        """
        UPDATE tasks
           SET closed_at = ?,
               closed_reason = ?,
               closed_in_session_id = ?,
               closed_commit_sha = ?,
               validation_override_reason = ?,
               escalated_at = NULL,
               escalation_reason = NULL,
               is_escalated = 0,
               assignee = NULL,
               claimed_by_session_id = NULL,
               updated_at = ?
         WHERE id = ?
        """,
        (
            now,
            reason,
            persisted_session_id,
            commit_sha,
            validation_override_reason,
            now,
            task_id,
        ),
    )
    if cascade_descendants:
        _cascade_close_descendants(conn, task_id, now, persisted_session_id, commit_sha)


def _session_exists(conn: sqlite3.Connection, session_id: str | None) -> bool:
    if not session_id:
        return False
    row = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row is not None


def _cascade_close_descendants(
    conn: sqlite3.Connection,
    task_id: str,
    closed_at: str,
    closed_in_session_id: str | None,
    commit_sha: str | None,
) -> None:
    rows = conn.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM tasks WHERE parent_task_id = ?
            UNION ALL
            SELECT tasks.id FROM tasks JOIN subtree ON tasks.parent_task_id = subtree.id
        )
        SELECT id FROM subtree
        """,
        (task_id,),
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            UPDATE tasks
               SET closed_at = ?,
                   closed_reason = 'merged',
                   closed_in_session_id = ?,
                   closed_commit_sha = ?,
                   assignee = NULL,
                   claimed_by_session_id = NULL,
                   updated_at = ?
             WHERE id = ?
            """,
            (closed_at, closed_in_session_id, commit_sha, closed_at, row["id"]),
        )


class StageStatesManager:
    def __init__(self, db: DatabaseProtocol, events: TaskLifecycleEventManager) -> None:
        self.db = db
        self.events = events
        self.registry = StageRegistryManager(db)
        self.mutexes = TaskDispatchMutexManager(db)
        self._ensure_phase2_columns()

    def list_for_task(self, task_id: str) -> list[StageState]:
        rows = self.db.fetchall(
            """
            SELECT *
              FROM task_stage_states
             WHERE task_id = ?
             ORDER BY position, stage_name
            """,
            (task_id,),
        )
        return [self._state_from_row(row) for row in rows]

    def get(self, task_id: str, stage_name: str) -> StageState | None:
        row = self.db.fetchone(
            """
            SELECT *
              FROM task_stage_states
             WHERE task_id = ? AND stage_name = ?
            """,
            (task_id, stage_name),
        )
        return self._state_from_row(row) if row is not None else None

    def current_stage(self, task_id: str) -> StageState | None:
        row = self.db.fetchone(
            """
            SELECT *
              FROM task_stage_states
             WHERE task_id = ? AND state != 'done'
             ORDER BY position, stage_name
             LIMIT 1
            """,
            (task_id,),
        )
        return self._state_from_row(row) if row is not None else None

    def list_tasks_at_stage(
        self,
        *,
        stage_name: str,
        state: str | None = None,
        project_id: str | None = None,
    ) -> list[str]:
        params: list[object] = [stage_name]
        filters = ["s.stage_name = ?"]
        if state is not None:
            self._validate_state_value(state)
            filters.append("s.state = ?")
            params.append(state)
        if project_id is not None:
            filters.append("t.project_id = ?")
            params.append(project_id)
        rows = self.db.fetchall(
            f"""
            SELECT s.task_id
              FROM task_stage_states s
              JOIN tasks t ON t.id = s.task_id
             WHERE {" AND ".join(filters)}
             ORDER BY t.created_at, s.task_id
            """,  # nosec B608 - filters are assembled from fixed clauses.
            tuple(params),
        )
        return [row["task_id"] for row in rows]

    def initialize_manifest(
        self,
        task_id: str,
        specs: Sequence[StageManifestSpec],
        *,
        by_session_id: str | None,
    ) -> list[StageState]:
        self._validate_specs(specs)
        holder = by_session_id or "system"
        snapshot = self.current_stage(task_id)
        with self._mutex(task_id, holder, "initialize_manifest", expected_stage=snapshot):
            existing = self.list_for_task(task_id)
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
                    registry = self._registry_entry(spec.stage_name)
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
                            registry.reviewer_agent,
                            spec.max_work_attempts,
                            spec.max_review_rounds,
                            now,
                        ),
                    )
                self.events.record_lifecycle_event(
                    task_id,
                    None,
                    f"manifest:{self._shape_signature_for_specs(specs)}",
                    "initialize_manifest",
                    by_actor=holder,
                )
            return self.list_for_task(task_id)

    def add_stage(
        self,
        task_id: str,
        spec: StageManifestSpec,
        *,
        by_session_id: str | None,
    ) -> StageState:
        holder = by_session_id or "system"
        snapshot = self.current_stage(task_id)
        with self._mutex(
            task_id,
            holder,
            f"{spec.stage_name}:add_stage",
            expected_stage=snapshot,
        ):
            current = self.current_stage(task_id)
            self._validate_add(task_id, spec, current)
            registry = self._registry_entry(spec.stage_name)
            previous_shape = self._shape_signature(task_id)
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
                        registry.reviewer_agent,
                        spec.max_work_attempts,
                        spec.max_review_rounds,
                        now,
                    ),
                )
                self._record_shape_event(task_id, previous_shape, "add_stage", holder)
            return self.get(task_id, spec.stage_name)  # type: ignore[return-value]

    def remove_stage(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
    ) -> None:
        holder = by_session_id or "system"
        snapshot = self.current_stage(task_id)
        with self._mutex(
            task_id,
            holder,
            f"{stage_name}:remove_stage",
            expected_stage=snapshot,
        ):
            row = self.get(task_id, stage_name)
            current = self.current_stage(task_id)
            self._validate_remove(task_id, stage_name, row, current)
            previous_shape = self._shape_signature(task_id)
            now = _now()
            assert row is not None
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
                self._record_shape_event(task_id, previous_shape, "remove_stage", holder)

    def start_stage(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
        notes: str | None = None,
    ) -> StageState:
        return self._transition(
            task_id,
            stage_name,
            "start_stage",
            by_session_id=by_session_id,
            notes=notes,
        )

    def submit_for_review(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
        notes: str | None = None,
    ) -> StageState:
        return self._transition(
            task_id,
            stage_name,
            "submit_for_review",
            by_session_id=by_session_id,
            notes=notes,
        )

    def approve_review(
        self,
        task_id: str,
        stage_name: str,
        *,
        by_session_id: str | None,
        notes: str | None = None,
    ) -> StageState:
        return self._transition(
            task_id,
            stage_name,
            "approve_review",
            by_session_id=by_session_id,
            notes=notes,
        )

    def reject_review(
        self,
        task_id: str,
        stage_name: str,
        *,
        reason: str,
        by_session_id: str | None,
        notes: str | None = None,
    ) -> StageState:
        return self._transition(
            task_id,
            stage_name,
            "reject_review",
            by_session_id=by_session_id,
            notes=notes,
            reason=reason,
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
    ) -> StageState:
        return self._transition(
            task_id,
            stage_name,
            "complete_stage",
            by_session_id=by_session_id,
            commit_sha=commit_sha,
            artifact_updates=artifact_updates,
            validation_override_reason=validation_override_reason,
        )

    def fail_stage(
        self,
        task_id: str,
        stage_name: str,
        *,
        reason: str,
        needs_human: bool = False,
        by_session_id: str | None,
    ) -> StageState:
        return self._transition(
            task_id,
            stage_name,
            "fail_stage",
            by_session_id=by_session_id,
            reason=reason,
            needs_human=needs_human,
        )

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
    ) -> StageState:
        holder = by_session_id or "system"
        snapshot = self.current_stage(task_id)
        with self._mutex(
            task_id,
            holder,
            f"{stage_name}:{verb}",
            expected_stage=snapshot,
        ):
            current = self.current_stage(task_id)
            row = self.get(task_id, stage_name)
            if row is None:
                raise ValueError(f"Stage '{stage_name}' is not in task manifest")
            from_state = row.state
            to_state, event_reason = self._transition_target(
                row,
                verb,
                reason=reason,
                validation_override_reason=validation_override_reason,
            )
            self._ensure_not_skipping(row, current, verb)

            now = _now()
            artifact_json = (
                json.dumps(dict(artifact_updates), sort_keys=True)
                if artifact_updates is not None
                else row.artifact_refs and json.dumps(row.artifact_refs, sort_keys=True)
            )
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE task_stage_states
                       SET state = ?,
                           entered_at = CASE WHEN ? = 'in_progress' THEN ? ELSE entered_at END,
                           entered_by_session_id = CASE
                               WHEN ? = 'in_progress' THEN ? ELSE entered_by_session_id
                           END,
                           completed_at = CASE WHEN ? = 'done' THEN ? ELSE completed_at END,
                           completed_by_session_id = CASE
                               WHEN ? = 'done' THEN ? ELSE completed_by_session_id
                           END,
                           completed_commit_sha = CASE
                               WHEN ? = 'done' THEN ? ELSE completed_commit_sha
                           END,
                           work_attempt_count = work_attempt_count + ?,
                           review_round_count = review_round_count + ?,
                           artifact_refs = COALESCE(?, artifact_refs),
                           notes = COALESCE(?, notes),
                           updated_at = ?
                     WHERE task_id = ? AND stage_name = ?
                    """,
                    (
                        to_state,
                        to_state,
                        now,
                        to_state,
                        holder,
                        to_state,
                        now,
                        to_state,
                        holder,
                        to_state,
                        commit_sha,
                        1 if verb == "start_stage" else 0,
                        1 if verb == "reject_review" else 0,
                        artifact_json,
                        notes,
                        now,
                        task_id,
                        stage_name,
                    ),
                )
                self.events.record_lifecycle_event(
                    task_id,
                    f"{stage_name}:{from_state}",
                    f"{stage_name}:{to_state}",
                    event_reason,
                    by_actor=holder,
                )
                if to_state == "done" and self._terminal_after_done(conn, task_id, stage_name):
                    _close_task_in_txn(
                        conn,
                        task_id,
                        db=self.db,
                        reason="manifest_exhausted",
                        commit_sha=commit_sha,
                        closed_at=now,
                        closed_in_session_id=by_session_id,
                        cascade_descendants=stage_name == "merge",
                        validation_override_reason=validation_override_reason,
                    )
            updated = self.get(task_id, stage_name)
            assert updated is not None
            if verb == "reject_review" and updated.review_round_count >= self._effective_cap(
                updated, "review"
            ):
                self._escalate(task_id, f"{stage_name}_review_failed:max")
            if verb == "fail_stage" and updated.work_attempt_count >= self._effective_cap(
                updated, "work"
            ):
                self._escalate(task_id, f"{stage_name}_work_failed:max")
            if verb == "fail_stage" and needs_human:
                self._escalate(task_id, f"{stage_name}_failed:{reason or 'needs_human'}")
            return updated

    def _transition_target(
        self,
        row: StageState,
        verb: str,
        *,
        reason: str | None,
        validation_override_reason: str | None,
    ) -> tuple[StageState5, str]:
        if verb == "start_stage":
            if row.state != "ready":
                raise self._illegal(row, verb)
            return "in_progress", "start_stage"
        if verb == "submit_for_review":
            if row.state != "in_progress" or row.review_policy == "none":
                raise self._illegal(row, verb)
            return "needs_review", "submit_for_review"
        if verb == "approve_review":
            if row.state != "needs_review" or row.review_policy == "none":
                raise self._illegal(row, verb)
            return "review_approved", "approve_review"
        if verb == "reject_review":
            if row.state != "needs_review" or row.review_policy == "none":
                raise self._illegal(row, verb)
            return "ready", "reject_review"
        if verb == "complete_stage":
            if row.state == "review_approved" and row.review_policy in {"required", "optional"}:
                return "done", "complete_stage"
            if row.state == "in_progress" and row.review_policy in {"none", "optional"}:
                return "done", "complete_stage"
            if (
                row.state == "in_progress"
                and row.review_policy == "required"
                and validation_override_reason
            ):
                return "done", f"validation_override:{validation_override_reason}"
            raise self._illegal(row, verb)
        if verb == "fail_stage":
            if row.state != "in_progress":
                raise self._illegal(row, verb)
            return "ready", "fail_stage"
        raise ValueError(f"Unknown stage transition '{verb}'")

    def _state_from_row(self, row: Any) -> StageState:
        return StageState(
            task_id=row["task_id"],
            stage_name=row["stage_name"],
            position=int(row["position"]),
            state=row["state"],
            review_policy=row["review_policy"],
            reviewer_agent=row["reviewer_agent"],
            entered_at=row["entered_at"],
            entered_by_session_id=row["entered_by_session_id"],
            completed_at=row["completed_at"],
            completed_by_session_id=row["completed_by_session_id"],
            completed_commit_sha=row["completed_commit_sha"],
            work_attempt_count=int(row["work_attempt_count"]),
            review_round_count=int(row["review_round_count"]),
            max_work_attempts=row["max_work_attempts"],
            max_review_rounds=row["max_review_rounds"],
            artifact_refs=_coerce_artifact_refs(row["artifact_refs"]),
            notes=row["notes"],
            updated_at=row["updated_at"],
        )

    def _ensure_phase2_columns(self) -> None:
        columns = self._columns("task_stage_states")
        table_sql = self.db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_stage_states'"
        )
        if "attempt_count" in columns or (
            table_sql is not None and "needs_review" not in str(table_sql["sql"])
        ):
            self._rebuild_stage_states_table()
            return
        additions = {
            "review_policy": (
                "ALTER TABLE task_stage_states ADD COLUMN review_policy TEXT "
                "NOT NULL DEFAULT 'none'"
            ),
            "reviewer_agent": "ALTER TABLE task_stage_states ADD COLUMN reviewer_agent TEXT",
            "work_attempt_count": (
                "ALTER TABLE task_stage_states ADD COLUMN work_attempt_count "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "review_round_count": (
                "ALTER TABLE task_stage_states ADD COLUMN review_round_count "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "max_work_attempts": (
                "ALTER TABLE task_stage_states ADD COLUMN max_work_attempts INTEGER"
            ),
            "max_review_rounds": (
                "ALTER TABLE task_stage_states ADD COLUMN max_review_rounds INTEGER"
            ),
        }
        with self.db.transaction() as conn:
            for column, sql in additions.items():
                if column not in columns:
                    conn.execute(sql)
            if "attempt_count" in columns and "work_attempt_count" not in columns:
                conn.execute(
                    """
                    UPDATE task_stage_states
                       SET work_attempt_count = COALESCE(attempt_count, 0)
                    """
                )

    def _rebuild_stage_states_table(self) -> None:
        rows = [dict(row) for row in self.db.fetchall("SELECT * FROM task_stage_states")]
        with self.db.transaction() as conn:
            conn.execute("DROP INDEX IF EXISTS idx_task_stage_states_position")
            conn.execute("DROP INDEX IF EXISTS idx_task_stage_states_state")
            conn.execute("DROP INDEX IF EXISTS idx_task_stage_states_open")
            conn.execute("DROP TABLE IF EXISTS task_stage_states")
            conn.execute(
                """
                CREATE TABLE task_stage_states (
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    stage_name TEXT NOT NULL
                        REFERENCES task_stages_registry(name) ON DELETE RESTRICT,
                    position INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'ready'
                        CHECK (state IN (
                            'ready','in_progress','needs_review','review_approved','done'
                        )),
                    review_policy TEXT NOT NULL DEFAULT 'none'
                        CHECK (review_policy IN ('none','required','optional')),
                    reviewer_agent TEXT,
                    entered_at TEXT,
                    entered_by_session_id TEXT,
                    completed_at TEXT,
                    completed_by_session_id TEXT,
                    completed_commit_sha TEXT,
                    work_attempt_count INTEGER NOT NULL DEFAULT 0,
                    review_round_count INTEGER NOT NULL DEFAULT 0,
                    max_work_attempts INTEGER,
                    max_review_rounds INTEGER,
                    artifact_refs TEXT,
                    notes TEXT,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (task_id, stage_name)
                )
                """
            )
            for row in rows:
                registry = self._registry_entry(row["stage_name"])
                conn.execute(
                    """
                    INSERT INTO task_stage_states (
                        task_id, stage_name, position, state, review_policy, reviewer_agent,
                        entered_at, entered_by_session_id, completed_at,
                        completed_by_session_id, completed_commit_sha, work_attempt_count,
                        review_round_count, max_work_attempts, max_review_rounds,
                        artifact_refs, notes, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["task_id"],
                        row["stage_name"],
                        row["position"],
                        row.get("state", "ready"),
                        row.get("review_policy") or registry.review_policy,
                        row.get("reviewer_agent") or registry.reviewer_agent,
                        row.get("entered_at"),
                        row.get("entered_by_session_id"),
                        row.get("completed_at"),
                        row.get("completed_by_session_id"),
                        row.get("completed_commit_sha"),
                        row.get("work_attempt_count", row.get("attempt_count", 0)) or 0,
                        row.get("review_round_count", 0) or 0,
                        row.get("max_work_attempts"),
                        row.get("max_review_rounds"),
                        row.get("artifact_refs"),
                        row.get("notes"),
                        row.get("updated_at") or _now(),
                    ),
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX idx_task_stage_states_position
                    ON task_stage_states (task_id, position)
                """
            )
            conn.execute(
                """
                CREATE INDEX idx_task_stage_states_state
                    ON task_stage_states (stage_name, state)
                """
            )
            conn.execute(
                """
                CREATE INDEX idx_task_stage_states_open
                    ON task_stage_states (task_id, position) WHERE state != 'done'
                """
            )

    def _columns(self, table_name: str) -> set[str]:
        return {row["name"] for row in self.db.fetchall(f"PRAGMA table_info({table_name})")}

    def _validate_specs(self, specs: Sequence[StageManifestSpec]) -> None:
        if not specs:
            raise ValueError("manifest must contain at least one stage")
        seen_names: set[str] = set()
        seen_positions: set[int] = set()
        for item in specs:
            if self.registry.get(item.stage_name) is None:
                raise ValueError(f"Unknown stage '{item.stage_name}'")
            if item.stage_name in seen_names:
                raise ValueError(f"Duplicate stage '{item.stage_name}'")
            if item.position in seen_positions:
                raise ValueError(f"Duplicate stage position {item.position}")
            seen_names.add(item.stage_name)
            seen_positions.add(item.position)

    def _registry_entry(self, stage_name: str) -> StageRegistryEntry:
        entry = self.registry.get(stage_name)
        if entry is None:
            raise ValueError(f"Unknown stage '{stage_name}'")
        return entry

    def _mutex(
        self,
        task_id: str,
        holder: str,
        action: str,
        *,
        expected_stage: StageState | None = None,
    ) -> RuntimeDispatchMutex:
        if expected_stage is None:
            return RuntimeDispatchMutex(
                storage=self.mutexes,
                task_id=task_id,
                holder=holder,
                action_kind=f"stage_state:{action}",
                ttl_seconds=30,
            )
        return RuntimeDispatchMutex(
            storage=self.mutexes,
            task_id=task_id,
            holder=holder,
            action_kind=f"stage_state:{action}",
            ttl_seconds=30,
            expected_stage_name=expected_stage.stage_name,
            expected_stage_state=self._snapshot_state(expected_stage.state),
            expected_stage_updated_at=expected_stage.updated_at,
            candidate_loader=self.current_stage,
        )

    @staticmethod
    def _snapshot_state(value: StageState5) -> RuntimeStageSnapshotState | None:
        if value in {"ready", "in_progress", "needs_review", "review_approved"}:
            return cast(RuntimeStageSnapshotState, value)
        return None

    @staticmethod
    def _validate_state_value(value: str) -> None:
        if value not in {"ready", "in_progress", "needs_review", "review_approved", "done"}:
            raise ValueError(f"Invalid stage state '{value}'")

    @staticmethod
    def _shape_signature_for_specs(specs: Sequence[StageManifestSpec]) -> str:
        return ",".join(f"{item.position}:{item.stage_name}:ready" for item in specs)

    def _shape_signature(self, task_id: str) -> str:
        return ",".join(
            f"{row.position}:{row.stage_name}:{row.state}" for row in self.list_for_task(task_id)
        )

    def _record_shape_event(
        self,
        task_id: str,
        previous_shape: str,
        reason: str,
        holder: str,
    ) -> None:
        self.events.record_lifecycle_event(
            task_id,
            f"manifest:{previous_shape}",
            f"manifest:{self._shape_signature(task_id)}",
            reason,
            by_actor=holder,
        )

    def _validate_add(
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
        if self.get(task_id, spec.stage_name) is not None:
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

    def _validate_remove(
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
                for item in self.list_for_task(task_id)
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

    def _ensure_not_skipping(
        self,
        row: StageState,
        current: StageState | None,
        verb: str,
    ) -> None:
        if verb == "start_stage" and (current is None or row.position != current.position):
            raise self._illegal(row, verb)

    def _effective_cap(self, row: StageState, kind: Literal["work", "review"]) -> int:
        registry = self._registry_entry(row.stage_name)
        if kind == "work":
            return row.max_work_attempts or registry.default_max_work_attempts
        return row.max_review_rounds or registry.default_max_review_rounds

    @staticmethod
    def _illegal(row: StageState, verb: str) -> IllegalStageTransitionError:
        return IllegalStageTransitionError(
            row.stage_name,
            row.state,
            verb,
            row.review_policy,
        )

    @staticmethod
    def _terminal_after_done(
        conn: sqlite3.Connection,
        task_id: str,
        stage_name: str,
    ) -> bool:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
              FROM task_stage_states
             WHERE task_id = ?
               AND state != 'done'
               AND stage_name != ?
            """,
            (task_id, stage_name),
        ).fetchone()
        return int(row["count"]) == 0

    def _escalate(self, task_id: str, reason: str) -> None:
        from gobby.storage.tasks._transitions import escalate_task  # noqa: PLC0415

        try:
            escalate_task(self.db, task_id, reason=reason)
        except ValueError:
            row = self.db.fetchone(
                "SELECT is_escalated, escalation_reason FROM tasks WHERE id = ?",
                (task_id,),
            )
            if row is not None and bool(row["is_escalated"]) and row["escalation_reason"] == reason:
                return
            logger.exception("failed to escalate task %s after stage failure", task_id)
            raise
        except Exception:
            logger.exception("failed to escalate task %s after stage failure", task_id)
            raise
