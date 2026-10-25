"""Epic scope of a team: the open tasks an epic team owns, and the overlaps admission refuses.

An epic is admitted in epic mode when it has open non-epic descendants at any depth. Those
leaves are read live, never snapshotted, so found-work leaves created under the epic join
without re-admission. Admission refuses an overlap: any ancestor or descendant with a live
team, any open descendant already claimed, and a non-epic descendant with open children.
A claim on a descendant of an epic team belongs to that team's seats only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from gobby.agents.isolation_models import title_slug
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import Task
from gobby.storage.team_records import EPIC_MODE, live_team_rows_sql
from gobby.storage.worktrees import Worktree, WorktreeStatus

EPIC_TASK_TYPE = "epic"
SINGLE_MODE = "single"


class ScopeRefused(Exception):
    """An admission would overlap another team; ``code`` names the refusal."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ScopedTask:
    """An open task inside an epic's scope."""

    id: str
    task_type: str
    parent_task_id: str | None
    claimed_by_session_id: str | None


@dataclass(frozen=True)
class ScopeTeam:
    """A live team: its owning execution, the task it is bound to, and its mode."""

    team_id: str
    task_id: str
    mode: str


def tree_is_live(tree: Worktree) -> bool:
    return tree.status == WorktreeStatus.ACTIVE.value and Path(tree.worktree_path).is_dir()


def epic_names(task: Task) -> tuple[str, str]:
    """The branch and worktree directory an epic team's tree takes from its title."""
    slug = title_slug(task.title)
    directory = f"wt-epic-{task.seq_num}" + (f"-{slug}" if slug else "")
    return f"epic/{task.seq_num}/main", directory


def open_descendants(db: HubDatabase, task_id: str) -> list[ScopedTask]:
    """Every open task below ``task_id``, at any depth."""
    rows = db.fetchall(
        """
        WITH RECURSIVE below(id) AS (
            SELECT id FROM tasks WHERE parent_task_id = %s
            UNION
            SELECT t.id FROM tasks t JOIN below ON t.parent_task_id = below.id
        )
        SELECT id, parent_task_id, task_type, claimed_by_session_id
        FROM tasks
        WHERE id IN (SELECT id FROM below) AND closed_at IS NULL
        ORDER BY id
        """,
        (task_id,),
    )
    return [
        ScopedTask(
            id=str(row["id"]),
            task_type=str(row["task_type"]),
            parent_task_id=_text(row["parent_task_id"]),
            claimed_by_session_id=_text(row["claimed_by_session_id"]),
        )
        for row in rows
    ]


def open_leaf_below(db: HubDatabase, epic_id: str, task_id: str) -> ScopedTask | None:
    """The open, non-epic task ``task_id`` below ``epic_id``, or None."""
    return next(
        (
            task
            for task in open_descendants(db, epic_id)
            if task.id == task_id and task.task_type != EPIC_TASK_TYPE
        ),
        None,
    )


def ancestor_ids(db: HubDatabase, task_id: str) -> list[str]:
    """Every ancestor of ``task_id``, open or closed, nearest or not."""
    rows = db.fetchall(
        """
        WITH RECURSIVE above(id) AS (
            SELECT parent_task_id FROM tasks WHERE id = %s AND parent_task_id IS NOT NULL
            UNION
            SELECT t.parent_task_id FROM tasks t JOIN above ON t.id = above.id
            WHERE t.parent_task_id IS NOT NULL
        )
        SELECT id FROM above
        """,
        (task_id,),
    )
    return [str(row["id"]) for row in rows]


def live_teams(db: HubDatabase, task_ids: Sequence[str]) -> dict[str, ScopeTeam]:
    """The live team bound to each of ``task_ids``, keyed by task id.

    Liveness is the record-level definition in ``live_team_rows_sql``, which blocker resolution
    reads too. The directory checks on a team's tree stay with the reuse and verify paths.
    """
    if not task_ids:
        return {}
    rows = db.fetchall(
        f"""
        SELECT live.team_id, live.task_id, live.mode
        FROM ({live_team_rows_sql()}) live
        WHERE live.task_id = ANY(%s)
        ORDER BY live.admit_id
        """,  # nosec B608
        (list(task_ids),),
    )
    teams: dict[str, ScopeTeam] = {}
    for row in rows:
        task_id = str(row["task_id"])
        teams[task_id] = ScopeTeam(
            team_id=str(row["team_id"]),
            task_id=task_id,
            mode=row["mode"] or SINGLE_MODE,
        )
    return teams


def admission_mode(db: HubDatabase, task: Task) -> str:
    """The mode admitting ``task`` runs in; raises ScopeRefused when an overlap refuses it."""
    ancestors = ancestor_ids(db, task.id)
    for ancestor, team in live_teams(db, ancestors).items():
        raise ScopeRefused(
            "team_overlap",
            f"task {task.id} sits under task {ancestor}, which has live team {team.team_id}",
        )
    if task.task_type != EPIC_TASK_TYPE:
        return SINGLE_MODE
    descendants = open_descendants(db, task.id)
    for team in live_teams(db, [d.id for d in descendants]).values():
        raise ScopeRefused(
            "team_overlap",
            f"task {team.task_id} under epic {task.id} has live team {team.team_id}",
        )
    for descendant in descendants:
        if descendant.claimed_by_session_id:
            raise ScopeRefused(
                "team_descendant_claimed",
                f"task {descendant.id} under epic {task.id} is claimed; release it first",
            )
    parents = {d.parent_task_id for d in descendants}
    for descendant in descendants:
        if descendant.task_type != EPIC_TASK_TYPE and descendant.id in parents:
            raise ScopeRefused(
                "team_epic_topology",
                f"task {descendant.id} under epic {task.id} is not a leaf: it has open "
                "children; the Orchestrator places found-work leaves under an epic",
            )
    if any(d.task_type != EPIC_TASK_TYPE for d in descendants):
        return EPIC_MODE
    return SINGLE_MODE


def epic_team_above(db: HubDatabase, task_id: str) -> ScopeTeam | None:
    """The live epic-mode team above ``task_id``, whose seats alone may claim it."""
    for team in live_teams(db, ancestor_ids(db, task_id)).values():
        if team.mode == EPIC_MODE:
            return team
    return None


def _text(value: object) -> str | None:
    return None if value is None else str(value)
