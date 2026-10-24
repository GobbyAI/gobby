"""Reset helpers for expansion output retry paths."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, cast

from gobby.storage.expansion_runs import ExpansionRun
from gobby.storage.hub.protocol import ExpansionApplyMutation, TaskSeqAllocation
from gobby.storage.tasks import Task


@dataclass(frozen=True)
class ResetExpansionOutputResult:
    """Summary returned after deleting generated expansion output."""

    parent_task_id: str
    run_id: str | None
    deleted_task_ids: list[str] = field(default_factory=list)

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
            return cast(ExpansionRun, run)
    return None


def find_apply_blocking_expansion_output(
    self: Any,
    parent_task_id: str,
) -> ExpansionRun | None:
    """Return the latest expansion run whose remaining output should block apply."""
    parent = self.task_manager.get_task(parent_task_id)
    for run in self.run_manager.list_for_task(parent.id, limit=50):
        target_ids = _target_task_ids(self, parent, run)
        if target_ids and _target_ids_block_apply(self, target_ids):
            return cast(ExpansionRun, run)
    return None


def reset_expansion_output(
    self: Any,
    parent_task_id: str,
    run_id: str | None = None,
    session_id: str | None = None,
) -> ResetExpansionOutputResult:
    """Delete generated task output for one expansion run without touching other descendants."""
    parent = self.task_manager.get_task(parent_task_id)
    deleted_ids: list[str] = []
    with self.task_manager.db.transaction_immediate(
        TaskSeqAllocation(project_id=parent.project_id)
    ) as conn:
        conn.acquire_additional_lock(ExpansionApplyMutation(parent_task_id=parent.id))
        run = _resolve_reset_run(self, parent, run_id)
        if run is None:
            self.task_manager.artifacts.set_artifact(parent.id, "expansion_run_id", None)
            return ResetExpansionOutputResult(
                parent_task_id=parent.id,
                run_id=run_id,
                deleted_task_ids=[],
            )

        target_ids = _target_task_ids(self, parent, run)
        ordered_ids = _bottom_up(self, target_ids)
        # Use the same leaf-before-ancestor order as close. Hold every task
        # and existing artifact row through validation and deletion; the task
        # locks also fence inserts of new child/artifact rows through their FKs.
        for task_id in ordered_ids:
            conn.execute("SELECT id FROM tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
            conn.execute(
                "SELECT task_id FROM task_artifacts WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
        _validate_reset_targets(self, target_ids)

        for task_id in ordered_ids:
            if self.task_manager.delete_task(task_id, unlink=True):
                deleted_ids.append(task_id)

        artifacts = self.task_manager.artifacts.get_artifacts(parent.id)
        if artifacts.expansion_run_id in {None, run.id}:
            self.task_manager.artifacts.set_artifact(parent.id, "expansion_run_id", None)
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
        return cast(ExpansionRun, run)
    return find_existing_expansion_output(self, parent.id)


def _target_task_ids(self: Any, parent: Task, run: ExpansionRun) -> set[str]:
    target_ids = {
        task_id for task_id in (run.created_task_ids or []) if _task_exists(self, task_id)
    }
    for task_id in list(target_ids):
        target_ids.update(_ancestor_ids_between(self, task_id, parent.id))
    target_ids.discard(parent.id)
    return target_ids


def _target_ids_block_apply(self: Any, target_ids: set[str]) -> bool:
    for task_id in target_ids:
        if not _is_closed_obsolete_task(self, task_id):
            return True
    children_by_parent = _child_rows_by_parent(self, target_ids)
    for task_id in target_ids:
        for child in children_by_parent.get(task_id, []):
            child_id = child["id"]
            if child_id not in target_ids and not _is_closed_obsolete_task(self, child_id):
                return True
    return False


def _is_closed_obsolete_task(self: Any, task_id: str) -> bool:
    try:
        task = self.task_manager.get_task(task_id)
    except ValueError:
        # Expansion-run metadata can outlive generated tasks after a previous
        # reset. Treat missing rows as active blockers outside the known target
        # set so apply does not silently overwrite unrelated descendants.
        return False
    return task.closed_at is not None and task.closed_reason == "moot"


def _task_exists(self: Any, task_id: str) -> bool:
    return self.db.fetchone("SELECT 1 FROM tasks WHERE id = %s", (task_id,)) is not None


def _ancestor_ids_between(self: Any, task_id: str, parent_task_id: str) -> set[str]:
    ancestors: set[str] = set()
    current = self.db.fetchone("SELECT parent_task_id FROM tasks WHERE id = %s", (task_id,))
    next_id = current["parent_task_id"] if current is not None else None
    while next_id and next_id != parent_task_id:
        row = self.db.fetchone("SELECT parent_task_id FROM tasks WHERE id = %s", (next_id,))
        if row is None:
            break
        ancestors.add(next_id)
        next_id = row["parent_task_id"]
    return ancestors


def _validate_reset_targets(self: Any, target_ids: set[str]) -> None:
    problems: list[str] = []
    children_by_parent = _child_rows_by_parent(self, target_ids)
    for task_id in sorted(target_ids):
        task = self.task_manager.get_task(task_id)
        ref = _task_ref(task)
        if task.claimed_by_session_id:
            problems.append(f"{ref} is claimed")
        if task.commits or task.closed_commit_sha:
            problems.append(f"{ref} has linked commits")
        if task.closed_at:
            problems.append(f"{ref} is closed")
        if task.is_escalated:
            problems.append(f"{ref} is escalated")
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
        for child in children_by_parent.get(task.id, []):
            if child["id"] not in target_ids:
                child_ref = f"#{child['seq_num']}" if child["seq_num"] else child["id"]
                problems.append(f"{ref} has non-expansion child {child_ref}")
    if problems:
        raise ValueError("Cannot reset expansion output: " + "; ".join(problems))


def _child_rows_by_parent(self: Any, parent_ids: set[str]) -> dict[str, list[Any]]:
    if not parent_ids:
        return {}
    ordered_parent_ids = sorted(parent_ids)
    placeholders = ", ".join("%s" for _ in ordered_parent_ids)
    rows = self.db.fetchall(
        f"""
        SELECT id, seq_num, parent_task_id
          FROM tasks
         WHERE parent_task_id IN ({placeholders})
        """,  # nosec B608 # placeholders are generated for a bound ID list.
        tuple(ordered_parent_ids),
    )
    children: dict[str, list[Any]] = {parent_id: [] for parent_id in ordered_parent_ids}
    for row in rows:
        # parent_task_id is constrained by the SQL `IN (...)` clause to a key
        # in `ordered_parent_ids`, so the bucket is guaranteed to exist.
        children[row["parent_task_id"]].append(row)
    return children


def _bottom_up(self: Any, target_ids: set[str]) -> list[str]:
    return sorted(target_ids, key=lambda task_id: _depth(self, task_id), reverse=True)


def _depth(self: Any, task_id: str) -> int:
    row = self.db.fetchone("SELECT path_cache FROM tasks WHERE id = %s", (task_id,))
    path_cache = row["path_cache"] if row is not None else None
    if not path_cache:
        return 0
    return len(str(path_cache).split("."))


def _task_ref(task: Task) -> str:
    return f"#{task.seq_num}" if task.seq_num is not None else task.id


__all__ = [
    "ResetExpansionOutputResult",
    "find_apply_blocking_expansion_output",
    "find_existing_expansion_output",
    "reset_expansion_output",
]
