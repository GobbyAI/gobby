"""Reset helpers for expansion output retry paths."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from gobby.storage.delivery import TaskDeliveryStateManager
from gobby.storage.expansion_runs import ExpansionRun
from gobby.storage.tasks import Task


@dataclass(frozen=True)
class ResetExpansionOutputResult:
    """Summary returned after deleting generated expansion output."""

    parent_task_id: str
    run_id: str | None
    deleted_task_ids: list[str] = field(default_factory=list)
    reset_stage: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_existing_expansion_output(
    self: Any,
    parent_task_id: str,
    *,
    run_id: str | None = None,
) -> ExpansionRun | None:
    """Return the latest/specified expansion run whose generated output still exists."""
    parent = self.task_manager.get_task(parent_task_id)
    if run_id is not None:
        run = self.run_manager.get(run_id)
        if run is None:
            raise ValueError(f"Expansion run {run_id} not found")
        if run.parent_task_id != parent.id:
            raise ValueError(f"Expansion run {run_id} does not belong to task {parent_task_id}")
        return run if _target_task_ids(self, parent, run) else None

    for run in self.run_manager.list_for_task(parent.id, limit=50):
        if _target_task_ids(self, parent, run):
            return run
    return None


def reset_expansion_output(
    self: Any,
    parent_task_id: str,
    run_id: str | None = None,
    session_id: str | None = None,
) -> ResetExpansionOutputResult:
    """Delete generated task output for one expansion run without touching other descendants."""
    parent = self.task_manager.get_task(parent_task_id)
    run = _resolve_reset_run(self, parent, run_id)
    if run is None:
        reset_stage = _reset_parent_expansion_stage(self, parent.id, session_id=session_id)
        self.task_manager.artifacts.set_artifact(parent.id, "expansion_run_id", None)
        return ResetExpansionOutputResult(
            parent_task_id=parent.id,
            run_id=run_id,
            deleted_task_ids=[],
            reset_stage=reset_stage,
        )

    target_ids = _target_task_ids(self, parent, run)
    _validate_reset_targets(self, target_ids)

    deleted_ids: list[str] = []
    for task_id in _bottom_up(self, target_ids):
        if self.task_manager.delete_task(task_id, unlink=True):
            deleted_ids.append(task_id)

    artifacts = self.task_manager.artifacts.get_artifacts(parent.id)
    if artifacts.expansion_run_id in {None, run.id}:
        self.task_manager.artifacts.set_artifact(parent.id, "expansion_run_id", None)
    reset_stage = _reset_parent_expansion_stage(self, parent.id, session_id=session_id)
    self.run_manager.append_log(
        run.id,
        level="info",
        message="Reset expansion output",
        extra={"deleted_task_ids": deleted_ids},
    )
    return ResetExpansionOutputResult(
        parent_task_id=parent.id,
        run_id=run.id,
        deleted_task_ids=deleted_ids,
        reset_stage=reset_stage,
    )


def _resolve_reset_run(
    self: Any,
    parent: Task,
    run_id: str | None,
) -> ExpansionRun | None:
    if run_id is not None:
        run = self.run_manager.get(run_id)
        if run is None:
            raise ValueError(f"Expansion run {run_id} not found")
        if run.parent_task_id != parent.id:
            raise ValueError(f"Expansion run {run_id} does not belong to task {parent.id}")
        return run
    return find_existing_expansion_output(self, parent.id)


def _target_task_ids(self: Any, parent: Task, run: ExpansionRun) -> set[str]:
    target_ids = {
        task_id for task_id in (run.created_task_ids or []) if _task_exists(self, task_id)
    }
    for task_id in list(target_ids):
        target_ids.update(_ancestor_ids_between(self, task_id, parent.id))
    target_ids.discard(parent.id)
    return target_ids


def _task_exists(self: Any, task_id: str) -> bool:
    return self.db.fetchone("SELECT 1 FROM tasks WHERE id = ?", (task_id,)) is not None


def _ancestor_ids_between(self: Any, task_id: str, parent_task_id: str) -> set[str]:
    ancestors: set[str] = set()
    current = self.db.fetchone("SELECT parent_task_id FROM tasks WHERE id = ?", (task_id,))
    next_id = current["parent_task_id"] if current is not None else None
    while next_id and next_id != parent_task_id:
        row = self.db.fetchone("SELECT parent_task_id FROM tasks WHERE id = ?", (next_id,))
        if row is None:
            break
        ancestors.add(next_id)
        next_id = row["parent_task_id"]
    return ancestors


def _validate_reset_targets(self: Any, target_ids: set[str]) -> None:
    problems: list[str] = []
    for task_id in sorted(target_ids):
        task = self.task_manager.get_task(task_id)
        ref = _task_ref(task)
        if task.claimed_by_session_id:
            problems.append(f"{ref} is claimed")
        if task.commits or task.closed_commit_sha:
            problems.append(f"{ref} has linked commits")
        if task.closed_at:
            problems.append(f"{ref} is closed")
        artifacts = self.task_manager.artifacts.get_artifacts(task.id)
        if any(
            (
                artifacts.worktree_path,
                artifacts.worktree_id,
                artifacts.clone_path,
                artifacts.clone_id,
            )
        ):
            problems.append(f"{ref} has isolation artifacts")
        delivery = TaskDeliveryStateManager(self.db).get_state(task.id)
        if delivery["campaign"] is not None or delivery["units"]:
            problems.append(f"{ref} has delivery state")
        if _has_progressed_stage_state(self, task.id):
            problems.append(f"{ref} has progressed stage state")
        outside_children = self.db.fetchall(
            """
            SELECT id, seq_num
              FROM tasks
             WHERE parent_task_id = ?
            """,
            (task.id,),
        )
        for child in outside_children:
            if child["id"] not in target_ids:
                child_ref = f"#{child['seq_num']}" if child["seq_num"] else child["id"]
                problems.append(f"{ref} has non-expansion child {child_ref}")
    if problems:
        raise ValueError("Cannot reset expansion output: " + "; ".join(problems))


def _has_progressed_stage_state(self: Any, task_id: str) -> bool:
    for row in self.task_manager.stage_states.list_for_task(task_id):
        if (
            row.state != "ready"
            or row.entered_at is not None
            or row.completed_at is not None
            or row.work_attempt_count
            or row.review_round_count
            or row.artifact_refs is not None
            or row.notes is not None
        ):
            return True
    return False


def _bottom_up(self: Any, target_ids: set[str]) -> list[str]:
    return sorted(target_ids, key=lambda task_id: _depth(self, task_id), reverse=True)


def _depth(self: Any, task_id: str) -> int:
    row = self.db.fetchone("SELECT path_cache FROM tasks WHERE id = ?", (task_id,))
    path_cache = row["path_cache"] if row is not None else None
    if not path_cache:
        return 0
    return len(str(path_cache).split("."))


def _reset_parent_expansion_stage(
    self: Any,
    parent_task_id: str,
    *,
    session_id: str | None,
) -> bool:
    row = self.task_manager.stage_states.get(parent_task_id, "expansion")
    if row is None:
        return False
    now = datetime.now(UTC).isoformat()
    holder = session_id or "system"
    with self.db.transaction() as conn:
        conn.execute(
            """
            UPDATE task_stage_states
               SET state = 'ready',
                   entered_at = NULL,
                   entered_by_session_id = NULL,
                   completed_at = NULL,
                   completed_by_session_id = NULL,
                   completed_commit_sha = NULL,
                   work_attempt_count = 0,
                   review_round_count = 0,
                   artifact_refs = NULL,
                   notes = NULL,
                   updated_at = ?
             WHERE task_id = ? AND stage_name = 'expansion'
            """,
            (now, parent_task_id),
        )
        self.task_manager.lifecycle_events.record_lifecycle_event(
            parent_task_id,
            f"expansion:{row.state}",
            "expansion:ready",
            "reset_expansion_output",
            by_actor=holder,
        )
    return True


def complete_parent_expansion_stage_if_current(
    self: Any,
    parent_task_id: str,
    *,
    session_id: str | None,
) -> bool:
    """Complete the parent expansion stage only when it is the active current stage."""
    row = self.task_manager.stage_states.get(parent_task_id, "expansion")
    if row is None or row.state == "done":
        return False
    current = self.task_manager.stage_states.current_stage(parent_task_id)
    if current is None or current.stage_name != "expansion":
        return False
    if row.state not in {"in_progress", "review_approved"}:
        return False
    override = "expansion_apply_completed" if row.state == "in_progress" else None
    self.task_manager.stage_states.complete_stage(
        parent_task_id,
        "expansion",
        by_session_id=session_id,
        validation_override_reason=override,
    )
    return True


def _task_ref(task: Task) -> str:
    return f"#{task.seq_num}" if task.seq_num is not None else task.id


__all__ = [
    "ResetExpansionOutputResult",
    "complete_parent_expansion_stage_if_current",
    "find_existing_expansion_output",
    "reset_expansion_output",
]
