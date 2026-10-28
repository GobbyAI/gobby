"""Epic-mode review state for an epic-bound team (addendum 8.4).

The review loop derives every leaf's state from tasks, step records and Git; nothing here
recounts transcripts. ``step_records.py`` stays team-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass

from gobby.agents.team_lifecycle import (
    TeamAdmissionRefused,
    TeamBinding,
    _admission_step,
    _binding,
    _merge,
    _recorded,
    team_binding,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.team_records import EPIC_MODE
from gobby.tasks.step_records import (
    TaskStep,
    approved_task_step,
    current_task_steps,
    list_task_step_records,
)
from gobby.utils.git import run_git_command


@dataclass(frozen=True)
class EpicReviewState:
    """An epic team's review state as of one pass.

    ``held`` names an explicit epic hold. ``holds`` lists blocked leaves and their
    blockers without stopping ready siblings. ``next_reviewers`` maps each reviewed
    leaf to its reviewer definition. A linked tip is ready when its merged leaves
    are approved and their dependencies are satisfied.
    """

    held: bool
    holds: tuple[str, ...]
    next_reviewers: dict[str, str]
    epic_tip_ready: bool


def _tip_blockers(db: HubDatabase, leaf_id: str, epic_leaf_ids: set[str]) -> set[str]:
    """Closed epic prerequisites still need evidence in this exact candidate tip."""
    rows = db.fetchall(
        """
        SELECT d.depends_on, blocker.closed_at
        FROM task_dependencies d
        JOIN tasks blocker ON blocker.id = d.depends_on
        WHERE d.task_id = %s AND d.dep_type = 'blocks'
        """,
        (leaf_id,),
    )
    return {
        str(row["depends_on"])
        for row in rows
        if row["closed_at"] is None or str(row["depends_on"]) in epic_leaf_ids
    }


def _epic_leaves(db: HubDatabase, epic_id: str) -> list[Task]:
    """Include closed pod leaves: their delivered commits still belong to the epic tip."""
    rows = db.fetchall(
        """
        WITH RECURSIVE below(id) AS (
            SELECT id FROM tasks WHERE parent_task_id = %s
            UNION
            SELECT t.id FROM tasks t JOIN below ON t.parent_task_id = below.id
        )
        SELECT * FROM tasks
        WHERE id IN (SELECT id FROM below) AND task_type != 'epic'
        ORDER BY id
        """,
        (epic_id,),
    )
    return [Task.from_row(row) for row in rows]


def _merged_tips(binding: TeamBinding, leaves: list[Task], tip: str | None) -> dict[str, str]:
    """Find each leaf's latest evidence included in the linked epic tip."""
    if not tip or not binding.worktree_path:
        return {}
    history = run_git_command(["git", "rev-list", tip, "--"], cwd=binding.worktree_path)
    if history is None:
        return {}
    ancestors = set(history.splitlines())
    merged: dict[str, str] = {}
    for leaf in leaves:
        for sha in reversed(leaf.commits or []):
            full = (
                sha
                if sha in ancestors
                else next((commit for commit in ancestors if commit.startswith(sha)), None)
            )
            if full is not None:
                merged[leaf.id] = full
                break
    return merged


def sync_epic_review(db: HubDatabase, team_id: str) -> EpicReviewState:
    """Derive readiness from the linked tip; a leaf rejection never escalates its epic."""
    executions = LocalPipelineExecutionManager(db, None)
    step = _admission_step(executions, team_id)
    record = _recorded(step)
    if step is None or record is None:
        raise TeamAdmissionRefused("team_missing", f"team {team_id} has no admission record")
    binding = _binding(record)
    if binding is None or binding.mode != EPIC_MODE:
        return EpicReviewState(held=False, holds=(), next_reviewers={}, epic_tip_ready=False)

    tasks = LocalTaskManager(db)
    epic = tasks.get_task(binding.task_id)
    leaves = _epic_leaves(db, epic.id)
    holds = {leaf.id for leaf in leaves if leaf.escalated_at is not None}
    if epic.escalated_at is not None:
        holds.add(epic.id)
    held = epic.escalated_at is not None
    if record.get("held") != held:
        _merge(executions, step, team={**record, "held": held})

    next_reviewers: dict[str, str] = {}
    tip = epic.commits[-1] if epic.commits else None
    merged = _merged_tips(binding, leaves, tip)
    approved: set[str] = set()
    approval_steps: tuple[TaskStep, ...] = ("implementation", "review")
    for leaf in leaves:
        records = list_task_step_records(db, leaf.id)
        review = current_task_steps(records, leaf.review_epoch).get("review")
        if review is not None:
            next_reviewers[leaf.id] = review.agent_definition
        if leaf.id in merged:
            included = current_task_steps(
                [record for record in records if record.commit_sha == merged[leaf.id]],
                leaf.review_epoch,
            )
            if all(
                (approval := included.get(step)) is not None and approval.status == "approved"
                for step in approval_steps
            ):
                approved.add(leaf.id)
        elif not leaf.commits and all(
            approved_task_step(leaf, records, step) is not None for step in approval_steps
        ):
            approved.add(leaf.id)

    epic_leaf_ids = {leaf.id for leaf in leaves}
    dependencies = {leaf.id: _tip_blockers(db, leaf.id, epic_leaf_ids) for leaf in leaves}
    # Propagate a held prerequisite through its dependents, never unrelated siblings.
    while True:
        previous = holds.copy()
        for leaf_id, blockers in dependencies.items():
            unresolved = {
                blocker for blocker in blockers if blocker not in approved or blocker in holds
            }
            if unresolved:
                holds.update(unresolved | {leaf_id})
        if holds == previous:
            break

    # The tip awaits a round when no current epic review already covers it, rejected or not.
    latest = current_task_steps(list_task_step_records(db, epic.id), epic.review_epoch).get(
        "review"
    )
    epic_tip_ready = (
        bool(merged)
        and all(leaf_id in approved and leaf_id not in holds for leaf_id in merged)
        and tip is not None
        and not held
        and (latest is None or latest.commit_sha != tip)
    )
    return EpicReviewState(
        held=held,
        holds=tuple(sorted(holds)),
        next_reviewers=next_reviewers,
        epic_tip_ready=epic_tip_ready,
    )


def seat_team_held(db: HubDatabase, binding: TeamBinding) -> bool:
    """Whether an epic team is held, read from the store without persisting anything.

    A leaf's rejection or dependency blocks that leaf, not the whole team.
    """
    if binding.parent_team_id is not None:
        parent = team_binding(db, binding.parent_team_id)
        if parent is None:
            return True
        binding = parent
    if binding.mode != EPIC_MODE:
        return False
    epic = LocalTaskManager(db).get_task(binding.task_id)
    return epic.escalated_at is not None
