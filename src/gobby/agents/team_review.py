"""Epic-mode review state for an epic-bound team (addendum 8.4).

The review loop derives every leaf's state from tasks, step records and Git; nothing here
recounts transcripts. ``step_records.py`` stays team-agnostic.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from gobby.agents.team_lifecycle import (
    TeamAdmissionRefused,
    _admission_step,
    _binding,
    _merge,
    _recorded,
)
from gobby.agents.team_scope import EPIC_TASK_TYPE, ancestor_ids, open_descendants
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.tasks import LocalTaskManager, TaskAlreadyEscalatedError
from gobby.storage.team_records import EPIC_MODE
from gobby.tasks.step_records import (
    approved_task_step,
    current_task_steps,
    list_task_step_records,
)


@dataclass(frozen=True)
class EpicReviewState:
    """An epic team's review state as of one pass.

    ``holds`` names each task holding the epic: an escalated leaf, an externally blocked
    leaf, or the escalated epic itself. ``next_reviewers`` maps each reviewed leaf to the
    reviewer definition whose seat reviews its next round. ``epic_tip_ready`` is true when
    every leaf has a current approval and a linked epic tip awaits its review round.
    """

    held: bool
    holds: tuple[str, ...]
    next_reviewers: dict[str, str]
    epic_tip_ready: bool


def _externally_blocked(db: HubDatabase, epic_id: str, leaf_id: str) -> bool:
    """Whether an open ``blocks`` dependency of the leaf lies outside the epic."""
    rows = db.fetchall(
        """
        SELECT d.depends_on
        FROM task_dependencies d
        JOIN tasks blocker ON blocker.id = d.depends_on
        WHERE d.task_id = %s AND d.dep_type = 'blocks' AND blocker.closed_at IS NULL
        """,
        (leaf_id,),
    )
    return any(epic_id not in ancestor_ids(db, str(row["depends_on"])) for row in rows)


def sync_epic_review(db: HubDatabase, team_id: str) -> EpicReviewState:
    """Derive team ``team_id``'s review state; escalate its epic once on a third rejection."""
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
    leaves = [
        tasks.get_task(scoped.id)
        for scoped in open_descendants(db, epic.id)
        if scoped.task_type != EPIC_TASK_TYPE
    ]
    exhausted = [leaf for leaf in leaves if leaf.escalated_at is not None]
    if exhausted and epic.escalated_at is None:
        with contextlib.suppress(TaskAlreadyEscalatedError):
            tasks.escalate_task(epic.id, f"Leaf {exhausted[0].id} exhausted its review rounds")
        epic = tasks.get_task(epic.id)

    holds = [
        leaf.id
        for leaf in leaves
        if leaf.escalated_at is not None or _externally_blocked(db, epic.id, leaf.id)
    ]
    if epic.escalated_at is not None:
        holds.append(epic.id)
    held = bool(holds)
    if record.get("held") != held:
        _merge(executions, step, team={**record, "held": held})

    next_reviewers: dict[str, str] = {}
    approvals_complete = True
    for leaf in leaves:
        records = list_task_step_records(db, leaf.id)
        review = current_task_steps(records, leaf.review_epoch).get("review")
        if review is not None:
            next_reviewers[leaf.id] = review.agent_definition
        approvals_complete = (
            approvals_complete and approved_task_step(leaf, records, "review") is not None
        )

    # The tip awaits a round when no current epic review already covers it, rejected or not.
    tip = epic.commits[-1] if epic.commits else None
    latest = current_task_steps(list_task_step_records(db, epic.id), epic.review_epoch).get(
        "review"
    )
    epic_tip_ready = (
        bool(leaves)
        and approvals_complete
        and tip is not None
        and not holds
        and (latest is None or latest.commit_sha != tip)
    )
    return EpicReviewState(
        held=held,
        holds=tuple(holds),
        next_reviewers=next_reviewers,
        epic_tip_ready=epic_tip_ready,
    )
