"""Ancestor-stage ordering gates for task automation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, cast

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._models import Task

CHILD_DEVELOPMENT_STAGE = "development"
CHILD_DEVELOPMENT_DISPATCH_STATES = frozenset(
    {"ready", "in_progress", "needs_review", "review_approved"}
)


@dataclass(frozen=True)
class AncestorStageGate:
    """Nearest ancestor stage that must finish before child development dispatch."""

    ancestor_task_id: str
    ancestor_ref: str
    ancestor_seq_num: int | None
    ancestor_path: str | None
    stage_name: str
    stage_state: str

    @property
    def reason(self) -> str:
        return "ancestor_stage_pending"

    def to_dict(self) -> dict[str, object]:
        return {"reason": self.reason, **asdict(self)}


def find_child_development_ancestor_gate(
    db: HubDatabase,
    task: Task,
    *,
    current_stage: object | None = None,
) -> AncestorStageGate | None:
    """Return the pending ancestor gate for a child development-stage task, if any."""
    stage = current_stage or _current_stage(task)
    if _stage_name(stage) != CHILD_DEVELOPMENT_STAGE:
        return None
    if _stage_state(stage) not in CHILD_DEVELOPMENT_DISPATCH_STATES:
        return None
    if not task.parent_task_id:
        return None

    row = db.fetchone(
        """
        WITH RECURSIVE ancestors(id, parent_task_id, seq_num, path_cache, depth) AS (
            SELECT parent.id, parent.parent_task_id, parent.seq_num, parent.path_cache, 1
              FROM tasks child
              JOIN tasks parent ON parent.id = child.parent_task_id
             WHERE child.id = ?
            UNION ALL
            SELECT parent.id,
                   parent.parent_task_id,
                   parent.seq_num,
                   parent.path_cache,
                   ancestors.depth + 1
              FROM tasks parent
              JOIN ancestors ON parent.id = ancestors.parent_task_id
        )
        SELECT ancestors.id AS ancestor_task_id,
               ancestors.seq_num AS ancestor_seq_num,
               ancestors.path_cache AS ancestor_path,
               current_stage.stage_name,
               current_stage.state AS stage_state
          FROM ancestors
          JOIN task_stage_states current_stage
            ON current_stage.task_id = ancestors.id
           AND current_stage.state != 'done'
           AND current_stage.position = (
               SELECT MIN(stage_scan.position)
                 FROM task_stage_states stage_scan
                WHERE stage_scan.task_id = ancestors.id
                  AND stage_scan.state != 'done'
           )
         WHERE current_stage.stage_name IN ('planning', 'expansion')
           AND current_stage.state IN ('ready', 'in_progress', 'needs_review', 'review_approved')
         ORDER BY ancestors.depth ASC
         LIMIT 1
        """,
        (task.id,),
    )
    if row is None:
        return None
    seq_num = row["ancestor_seq_num"]
    return AncestorStageGate(
        ancestor_task_id=str(row["ancestor_task_id"]),
        ancestor_ref=f"#{seq_num}" if seq_num is not None else str(row["ancestor_task_id"]),
        ancestor_seq_num=int(seq_num) if seq_num is not None else None,
        ancestor_path=row["ancestor_path"],
        stage_name=str(row["stage_name"]),
        stage_state=str(row["stage_state"]),
    )


def _current_stage(task: Task) -> object | None:
    pending = [stage for stage in task.stages if _stage_state(stage) != "done"]
    if not pending:
        return None
    return cast(object, min(pending, key=lambda stage: int(_field(stage, "position", 0) or 0)))


def _stage_name(stage: object | None) -> str:
    if stage is None:
        return ""
    return str(_field(stage, "stage_name", _field(stage, "name", "")))


def _stage_state(stage: object | None) -> str:
    if stage is None:
        return ""
    return str(_field(stage, "state", ""))


def _field(obj: object, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
