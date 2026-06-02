"""Holistic QA descendant gate detection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, cast

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._models import Task

HOLISTIC_QA_STAGE = "holistic_qa"
HOLISTIC_QA_GATE_STATES = frozenset({"ready", "in_progress"})
HOLISTIC_DESCENDANT_GATE_REASON = "holistic_descendants_nonterminal"


@dataclass(frozen=True)
class HolisticDescendantBlocker:
    task_id: str
    task_ref: str
    task_seq_num: int | None
    task_path: str | None
    title: str
    stage_name: str | None
    stage_state: str | None
    is_escalated: bool
    escalation_reason: str | None

    @property
    def reason(self) -> str:
        return HOLISTIC_DESCENDANT_GATE_REASON

    def to_dict(self) -> dict[str, object]:
        return {"reason": self.reason, **asdict(self)}


@dataclass(frozen=True)
class HolisticDescendantGate:
    root_task_id: str
    root_ref: str
    root_seq_num: int | None
    root_path: str | None
    stage_name: str
    stage_state: str
    blockers: tuple[HolisticDescendantBlocker, ...]

    @property
    def reason(self) -> str:
        return HOLISTIC_DESCENDANT_GATE_REASON

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "root_task_id": self.root_task_id,
            "root_ref": self.root_ref,
            "root_seq_num": self.root_seq_num,
            "root_path": self.root_path,
            "stage_name": self.stage_name,
            "stage_state": self.stage_state,
            "blockers": [blocker.to_dict() for blocker in self.blockers],
        }


def find_holistic_descendant_gate(
    db: HubDatabase,
    task: Task,
    *,
    current_stage: object | None = None,
) -> HolisticDescendantGate | None:
    """Return descendant blockers that prevent root holistic QA dispatch."""
    stage = current_stage or _current_stage(task)
    stage_name = _stage_name(stage)
    stage_state = _stage_state(stage)
    if task.task_type != "epic":
        return None
    if stage_name != HOLISTIC_QA_STAGE or stage_state not in HOLISTIC_QA_GATE_STATES:
        return None

    rows = db.fetchall(
        """
        WITH RECURSIVE descendants(id, depth) AS (
            SELECT child.id, 1
              FROM tasks child
             WHERE child.parent_task_id = %s
            UNION ALL
            SELECT child.id, descendants.depth + 1
              FROM tasks child
              JOIN descendants ON child.parent_task_id = descendants.id
        ),
        current_stage AS (
            SELECT stage_scan.task_id,
                   stage_scan.stage_name,
                   stage_scan.state
              FROM task_stage_states stage_scan
              JOIN descendants ON descendants.id = stage_scan.task_id
             WHERE stage_scan.state != 'done'
               AND stage_scan.position = (
                   SELECT MIN(stage_min.position)
                     FROM task_stage_states stage_min
                    WHERE stage_min.task_id = stage_scan.task_id
                      AND stage_min.state != 'done'
               )
        )
        SELECT tasks.id AS task_id,
               tasks.seq_num,
               tasks.path_cache,
               tasks.title,
               current_stage.stage_name,
               current_stage.state AS stage_state,
               tasks.escalated_at,
               tasks.escalation_reason,
               COALESCE(tasks.is_escalated, FALSE) AS is_escalated
          FROM descendants
          JOIN tasks ON tasks.id = descendants.id
          LEFT JOIN current_stage ON current_stage.task_id = tasks.id
         WHERE tasks.closed_at IS NULL
           AND (
               COALESCE(tasks.is_escalated, FALSE) IS TRUE
               OR tasks.escalated_at IS NOT NULL
               OR current_stage.task_id IS NOT NULL
           )
         ORDER BY descendants.depth ASC,
                  tasks.path_cache ASC NULLS LAST,
                  tasks.seq_num ASC NULLS LAST,
                  tasks.created_at ASC
        """,
        (task.id,),
    )
    blockers = tuple(_blocker_from_row(row) for row in rows)
    if not blockers:
        return None
    seq_num = task.seq_num
    return HolisticDescendantGate(
        root_task_id=task.id,
        root_ref=f"#{seq_num}" if seq_num is not None else task.id,
        root_seq_num=seq_num,
        root_path=task.path_cache,
        stage_name=stage_name,
        stage_state=stage_state,
        blockers=blockers,
    )


def has_holistic_ancestor_gate(
    db: HubDatabase,
    task: Task,
    *,
    current_stage: object | None = None,
) -> bool:
    """Return whether an ancestor epic is currently waiting in holistic QA."""
    stage = current_stage or _current_stage(task)
    if _stage_state(stage) == "done" or not task.parent_task_id:
        return False

    gate_states = tuple(sorted(HOLISTIC_QA_GATE_STATES))
    state_placeholders = ", ".join(["%s"] * len(gate_states))
    row = db.fetchone(
        f"""
        WITH RECURSIVE ancestors(id, parent_task_id, depth) AS (
            SELECT parent.id, parent.parent_task_id, 1
              FROM tasks child
              JOIN tasks parent ON parent.id = child.parent_task_id
             WHERE child.id = %s
            UNION ALL
            SELECT parent.id, parent.parent_task_id, ancestors.depth + 1
              FROM tasks parent
              JOIN ancestors ON parent.id = ancestors.parent_task_id
        )
        SELECT ancestors.id
          FROM ancestors
          JOIN tasks ancestor_task ON ancestor_task.id = ancestors.id
          JOIN task_stage_states current_stage
            ON current_stage.task_id = ancestors.id
           AND current_stage.state != 'done'
           AND current_stage.position = (
               SELECT MIN(stage_scan.position)
                 FROM task_stage_states stage_scan
                WHERE stage_scan.task_id = ancestors.id
                  AND stage_scan.state != 'done'
           )
         WHERE ancestor_task.task_type = 'epic'
           AND current_stage.stage_name = %s
           AND current_stage.state IN ({state_placeholders})
         LIMIT 1
        """,  # nosec B608 - placeholders are generated from an internal fixed-size state set.
        (task.id, HOLISTIC_QA_STAGE, *gate_states),
    )
    return row is not None


def _blocker_from_row(row: Mapping[str, Any]) -> HolisticDescendantBlocker:
    seq_num = row["seq_num"]
    task_id = str(row["task_id"])
    return HolisticDescendantBlocker(
        task_id=task_id,
        task_ref=f"#{seq_num}" if seq_num is not None else task_id,
        task_seq_num=int(seq_num) if seq_num is not None else None,
        task_path=row["path_cache"],
        title=str(row["title"]),
        stage_name=cast(str | None, row["stage_name"]),
        stage_state=cast(str | None, row["stage_state"]),
        is_escalated=bool(row["is_escalated"] or row["escalated_at"]),
        escalation_reason=cast(str | None, row["escalation_reason"]),
    )


def _current_stage(task: Task) -> object | None:
    return next((stage for stage in task.stages if _stage_state(stage) != "done"), None)


def _stage_name(stage: object | None) -> str:
    return cast(str, _field(stage, "stage_name", ""))


def _stage_state(stage: object | None) -> str:
    return cast(str, _field(stage, "state", ""))


def _field(obj: object | None, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
