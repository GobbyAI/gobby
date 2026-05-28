"""Transition engine for persisted task stage-state rows."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Literal

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.tasks._dispatcher_wake import wake_dispatcher_for_task_change
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._stage_state_mutex import StageStateMutexFactory
from gobby.storage.tasks._stage_state_rows import StageStateRows
from gobby.storage.tasks._stage_types import IllegalStageTransitionError, StageState, StageState5
from gobby.storage.tasks._stage_utils import _close_task_in_txn, _now
from gobby.utils.sql import sql_placeholders

logger = logging.getLogger(__name__)


class StageStateTransitions:
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

    def transition(
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
    ) -> StageState:
        holder = by_session_id or "system"
        snapshot = self.rows.current_stage(task_id)
        with self.mutexes.mutex(
            task_id,
            holder,
            f"{stage_name}:{verb}",
            expected_stage=snapshot,
        ):
            current = self.rows.current_stage(task_id)
            row = self.rows.get(task_id, stage_name)
            if row is None:
                raise ValueError(f"Stage '{stage_name}' is not in task manifest")
            from_state = row.state
            to_state, event_reason = self.transition_target(
                row,
                verb,
                reason=reason,
                validation_override_reason=validation_override_reason,
            )
            self.ensure_not_skipping(row, current, verb)
            retry_neutral_cited_failure = self._is_retry_neutral_cited_holistic_failure(
                stage_name,
                verb,
                reason=reason,
                cited_subtasks=cited_subtasks,
            )

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
                       SET state = %s,
                           entered_at = CASE WHEN %s = 'in_progress' THEN %s ELSE entered_at END,
                           entered_by_session_id = CASE
                               WHEN %s = 'in_progress' THEN %s ELSE entered_by_session_id
                           END,
                           completed_at = CASE WHEN %s = 'done' THEN %s ELSE completed_at END,
                           completed_by_session_id = CASE
                               WHEN %s = 'done' THEN %s ELSE completed_by_session_id
                           END,
                           completed_commit_sha = CASE
                               WHEN %s = 'done' THEN %s ELSE completed_commit_sha
                           END,
                           work_attempt_count = work_attempt_count + %s,
                           review_round_count = review_round_count + %s,
                           artifact_refs = COALESCE(%s, artifact_refs),
                           notes = COALESCE(%s, notes),
                           updated_at = %s
                     WHERE task_id = %s AND stage_name = %s
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
                if verb == "fail_stage" and stage_name == "holistic_qa" and cited_subtasks:
                    self.reset_holistic_failure_targets(
                        conn,
                        task_id,
                        tuple(cited_subtasks),
                        reason=reason,
                        now=now,
                        holder=holder,
                        reset_cited_work_attempts=retry_neutral_cited_failure,
                    )
                    if retry_neutral_cited_failure:
                        self.decrement_work_attempt(conn, task_id, stage_name, now=now)
                if to_state == "done" and terminal_after_done(conn, task_id, stage_name):
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
            updated = self.rows.get(task_id, stage_name)
            if updated is None:
                raise RuntimeError(f"Stage '{stage_name}' disappeared after transition")
            if verb == "reject_review" and updated.review_round_count >= self.effective_cap(
                updated, "review"
            ):
                self.escalate_stage_failure(task_id, f"{stage_name}_review_failed:max")
            if (
                verb == "fail_stage"
                and not retry_neutral_cited_failure
                and updated.work_attempt_count >= self.effective_cap(updated, "work")
            ):
                self.escalate_stage_failure(task_id, f"{stage_name}_work_failed:max")
            if verb == "fail_stage" and needs_human:
                self.escalate_stage_failure(
                    task_id,
                    f"{stage_name}_failed:{reason or 'needs_human'}",
                )
            self._wake_dispatcher(task_id, stage_name, verb)
            return updated

    @staticmethod
    def _is_retry_neutral_cited_holistic_failure(
        stage_name: str,
        verb: str,
        *,
        reason: str | None,
        cited_subtasks: Sequence[str] | None,
    ) -> bool:
        return (
            verb == "fail_stage"
            and stage_name == "holistic_qa"
            and bool(cited_subtasks)
            and bool(reason)
            and reason.startswith("dispatch_spawn_failed:")
        )

    def _wake_dispatcher(self, task_id: str, stage_name: str, verb: str) -> None:
        try:
            wake_dispatcher_for_task_change(self.db, task_id)
        except Exception:
            logger.warning(
                "dispatcher_wake_after_stage_transition_failed",
                extra={"task_id": task_id, "stage_name": stage_name, "verb": verb},
                exc_info=True,
            )

    def reset_holistic_failure_targets(
        self,
        conn: Transaction,
        task_id: str,
        cited_subtasks: Sequence[str],
        *,
        reason: str | None,
        now: str,
        holder: str,
        reset_cited_work_attempts: bool = False,
    ) -> None:
        cited_ids = tuple(dict.fromkeys(cited_subtasks))
        if not cited_ids:
            return
        placeholders = sql_placeholders(len(cited_ids))
        rows = conn.execute(
            f"""
            WITH RECURSIVE subtree(id) AS (
                SELECT id FROM tasks WHERE parent_task_id = %s
                UNION ALL
                SELECT tasks.id
                  FROM tasks
                  JOIN subtree ON tasks.parent_task_id = subtree.id
            )
            SELECT id FROM subtree WHERE id IN ({placeholders})
            """,  # nosec B608 # placeholder count is derived from cited_ids length.
            (task_id, *cited_ids),
        ).fetchall()
        descendant_ids = {str(row["id"]) for row in rows}
        missing = [cited_id for cited_id in cited_ids if cited_id not in descendant_ids]
        if missing:
            raise ValueError(
                "holistic_qa cited_subtasks must be descendants of the reviewed epic: "
                + ", ".join(missing)
            )

        self.append_holistic_failure_comments(
            conn,
            task_id,
            cited_ids,
            reason=reason,
            now=now,
            holder=holder,
        )
        self.reset_task_from_stage(conn, task_id, "development", now=now, holder=holder)
        for cited_id in cited_ids:
            self.reset_task_from_stage(
                conn,
                cited_id,
                "development",
                now=now,
                holder=holder,
                reset_work_attempts=reset_cited_work_attempts,
            )
        self.reactivate_cited_worktrees(conn, cited_ids, now=now)

    def decrement_work_attempt(
        self,
        conn: Transaction,
        task_id: str,
        stage_name: str,
        *,
        now: str,
    ) -> None:
        conn.execute(
            """
            UPDATE task_stage_states
               SET work_attempt_count = CASE
                       WHEN work_attempt_count > 0 THEN work_attempt_count - 1
                       ELSE 0
                   END,
                   updated_at = %s
             WHERE task_id = %s AND stage_name = %s
            """,
            (now, task_id, stage_name),
        )

    def reactivate_cited_worktrees(
        self,
        conn: Transaction,
        cited_subtasks: Sequence[str],
        *,
        now: str,
    ) -> None:
        if not cited_subtasks:
            return
        placeholders = sql_placeholders(len(cited_subtasks))
        conn.execute(
            f"""
            UPDATE worktrees
               SET status = 'active',
                   merged_at = NULL,
                   cleanup_after = NULL,
                   updated_at = %s
             WHERE task_id IN ({placeholders})
               AND status = 'merged'
            """,  # nosec B608 # placeholder count is derived from cited_subtasks length.
            (now, *cited_subtasks),
        )

    def append_holistic_failure_comments(
        self,
        conn: Transaction,
        task_id: str,
        cited_subtasks: Sequence[str],
        *,
        reason: str | None,
        now: str,
        holder: str,
    ) -> None:
        body = reason or "Holistic QA requested follow-up work."
        parent_body = f"## Holistic QA Failure\n\n{body}"
        self.append_task_comment(
            conn,
            task_id,
            body=parent_body,
            now=now,
            holder=holder,
        )
        follow_up_body = (
            "## Holistic QA Follow-Up\n\n"
            "This task was reopened because parent holistic QA requested changes.\n\n"
            f"{body}"
        )
        for cited_id in cited_subtasks:
            self.append_task_comment(
                conn,
                cited_id,
                body=follow_up_body,
                now=now,
                holder=holder,
            )

    def append_task_comment(
        self,
        conn: Transaction,
        task_id: str,
        *,
        body: str,
        now: str,
        holder: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_comments (
                id, task_id, parent_comment_id, author, author_type, body, created_at, updated_at
            )
            VALUES (%s, %s, NULL, %s, 'system', %s, %s, %s)
            """,
            (str(uuid.uuid4()), task_id, holder, body, now, now),
        )

    def reset_task_from_stage(
        self,
        conn: Transaction,
        task_id: str,
        stage_name: str,
        *,
        now: str,
        holder: str,
        reset_work_attempts: bool = False,
    ) -> None:
        stages = conn.execute(
            """
            SELECT stage_name, position, state
              FROM task_stage_states
             WHERE task_id = %s
             ORDER BY position, stage_name
            """,
            (task_id,),
        ).fetchall()
        if not stages:
            return
        reset_row = next((row for row in stages if row["stage_name"] == stage_name), None)
        if reset_row is None:
            reset_row = stages[0]
        reset_position = int(reset_row["position"])

        conn.execute(
            """
            UPDATE tasks
               SET closed_at = NULL,
                   closed_reason = NULL,
                   closed_in_session_id = NULL,
                   closed_commit_sha = NULL,
                   escalated_at = NULL,
                   escalation_reason = NULL,
                   is_escalated = FALSE,
                   assignee = NULL,
                   claimed_by_session_id = NULL,
                   validation_fail_count = 0,
                   dispatch_failure_count = 0,
                   updated_at = %s
             WHERE id = %s
            """,
            (now, task_id),
        )
        conn.execute(
            """
            UPDATE task_stage_states
               SET state = 'ready',
                   entered_at = NULL,
                   entered_by_session_id = NULL,
                   completed_at = NULL,
                   completed_by_session_id = NULL,
                   completed_commit_sha = NULL,
                   artifact_refs = NULL,
                   notes = NULL,
                   work_attempt_count = CASE WHEN %s THEN 0 ELSE work_attempt_count END,
                   updated_at = %s
             WHERE task_id = %s AND position >= %s
            """,
            (reset_work_attempts, now, task_id, reset_position),
        )
        for row in stages:
            if int(row["position"]) < reset_position or row["state"] == "ready":
                continue
            self.events.record_lifecycle_event(
                task_id,
                f"{row['stage_name']}:{row['state']}",
                f"{row['stage_name']}:ready",
                "holistic_qa_failed:cited_subtask",
                by_actor=holder,
            )

    def move_task_to_stage(
        self,
        task_id: str,
        target_stage_name: str,
        *,
        by_session_id: str | None,
        notes: str | None = None,
        force: bool = False,
    ) -> StageState:
        holder = by_session_id or "system"
        snapshot = self.rows.current_stage(task_id)
        with self.mutexes.mutex(
            task_id,
            holder,
            f"{target_stage_name}:move_to_stage",
            expected_stage=snapshot,
        ):
            stages = self.rows.list_for_task(task_id)
            target = next(
                (row for row in stages if row.stage_name == target_stage_name),
                None,
            )
            if target is None:
                raise ValueError(f"Stage '{target_stage_name}' is not in task manifest")

            now = _now()
            with self.db.transaction() as conn:
                task_row = conn.execute(
                    "SELECT claimed_by_session_id FROM tasks WHERE id = %s",
                    (task_id,),
                ).fetchone()
                claimed_by_session_id = task_row["claimed_by_session_id"] if task_row else None
                if claimed_by_session_id and claimed_by_session_id != by_session_id and not force:
                    raise ValueError(
                        "Task is claimed by another session; pass force=True to move stages"
                    )
                preserved_claim = (
                    claimed_by_session_id
                    if claimed_by_session_id and claimed_by_session_id == by_session_id
                    else None
                )
                conn.execute(
                    """
                    UPDATE tasks
                       SET closed_at = NULL,
                           closed_reason = NULL,
                           closed_in_session_id = NULL,
                           closed_commit_sha = NULL,
                           escalated_at = NULL,
                           escalation_reason = NULL,
                           is_escalated = FALSE,
                           assignee = NULL,
                           claimed_by_session_id = %s,
                           validation_fail_count = 0,
                           dispatch_failure_count = 0,
                           updated_at = %s
                     WHERE id = %s
                    """,
                    (preserved_claim, now, task_id),
                )

                for row in stages:
                    to_state: StageState5 = "done" if row.position < target.position else "ready"
                    if row.position < target.position:
                        conn.execute(
                            """
                            UPDATE task_stage_states
                               SET state = %s,
                                   completed_at = CASE
                                       WHEN state != %s THEN %s ELSE completed_at
                                   END,
                                   completed_by_session_id = CASE
                                       WHEN state != %s THEN %s ELSE completed_by_session_id
                                   END,
                                   completed_commit_sha = CASE
                                       WHEN state != %s THEN NULL ELSE completed_commit_sha
                                   END,
                                   updated_at = %s
                             WHERE task_id = %s AND stage_name = %s
                            """,
                            (
                                to_state,
                                to_state,
                                now,
                                to_state,
                                holder,
                                to_state,
                                now,
                                task_id,
                                row.stage_name,
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE task_stage_states
                               SET state = %s,
                                   entered_at = NULL,
                                   entered_by_session_id = NULL,
                                   completed_at = NULL,
                                   completed_by_session_id = NULL,
                                   completed_commit_sha = NULL,
                                   artifact_refs = NULL,
                                   notes = CASE
                                       WHEN stage_name = %s THEN %s
                                       ELSE NULL
                                   END,
                                   updated_at = %s
                             WHERE task_id = %s AND stage_name = %s
                            """,
                            (to_state, target_stage_name, notes, now, task_id, row.stage_name),
                        )
                    if row.state == to_state:
                        continue
                    self.events.record_lifecycle_event(
                        task_id,
                        f"{row.stage_name}:{row.state}",
                        f"{row.stage_name}:{to_state}",
                        f"move_to_stage:{target_stage_name}",
                        by_actor=holder,
                    )

            updated = self.rows.get(task_id, target_stage_name)
            if updated is None:
                raise RuntimeError(f"Stage '{target_stage_name}' disappeared after move_to_stage")
            return updated

    def transition_target(
        self,
        row: StageState,
        verb: str,
        *,
        reason: str | None,
        validation_override_reason: str | None,
    ) -> tuple[StageState5, str]:
        if verb == "start_stage":
            if row.state != "ready":
                raise illegal(row, verb)
            return "in_progress", "start_stage"
        if verb == "submit_for_review":
            if row.state != "in_progress" or row.review_policy == "none":
                raise illegal(row, verb)
            return "needs_review", "submit_for_review"
        if verb == "approve_review":
            if row.state != "needs_review" or row.review_policy == "none":
                raise illegal(row, verb)
            return "review_approved", "approve_review"
        if verb == "reject_review":
            if row.state != "needs_review" or row.review_policy == "none":
                raise illegal(row, verb)
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
            raise illegal(row, verb)
        if verb == "fail_stage":
            if row.state != "in_progress":
                raise illegal(row, verb)
            return "ready", "fail_stage"
        raise ValueError(f"Unknown stage transition '{verb}'")

    def ensure_not_skipping(
        self,
        row: StageState,
        current: StageState | None,
        verb: str,
    ) -> None:
        if verb == "start_stage" and (current is None or row.position != current.position):
            raise illegal(row, verb)

    def effective_cap(self, row: StageState, kind: Literal["work", "review"]) -> int:
        registry = self.rows.registry_entry(row.stage_name)
        if kind == "work":
            return row.max_work_attempts or registry.default_max_work_attempts
        return row.max_review_rounds or registry.default_max_review_rounds

    def escalate_stage_failure(self, task_id: str, reason: str) -> None:
        """Escalate a task for a stage failure, treating duplicate reasons as idempotent."""
        from gobby.storage.tasks import TaskAlreadyEscalatedError
        from gobby.storage.tasks._transitions import escalate_task

        try:
            escalate_task(self.db, task_id, reason=reason)
        except TaskAlreadyEscalatedError as exc:
            if exc.reason == reason:
                return
            logger.exception("failed to escalate task %s after stage failure", task_id)
            raise
        except ValueError:
            logger.exception("failed to escalate task %s after stage failure", task_id)
            raise
        except Exception:
            logger.exception("failed to escalate task %s after stage failure", task_id)
            raise


def illegal(row: StageState, verb: str) -> IllegalStageTransitionError:
    return IllegalStageTransitionError(
        row.stage_name,
        row.state,
        verb,
        row.review_policy,
    )


def terminal_after_done(
    conn: Transaction,
    task_id: str,
    stage_name: str,
) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
          FROM task_stage_states
         WHERE task_id = %s
           AND state != 'done'
           AND stage_name != %s
        """,
        (task_id, stage_name),
    ).fetchone()
    return bool(row is not None and int(row["count"]) == 0)
