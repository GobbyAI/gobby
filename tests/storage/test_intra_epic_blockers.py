"""Intra-epic blockers resolve on finished leaves inside an active epic team (addendum 8.3).

Three views must agree on every case: the SQL state projection, hydration's active-blocker
map, and ``approved_task_step(blocker, records, "implementation")``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._blocking import _fetch_active_blocker_map
from gobby.storage.tasks._queries import projected_task_state_sql
from gobby.tasks.step_records import (
    StepStatus,
    StepSubmission,
    TaskStep,
    approved_task_step,
    list_task_step_records,
    record_task_step,
)
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.review_approvals import grant_step_writes
from tests.fixtures.team_case import TeamWorld, make_team_world

pytestmark = pytest.mark.unit

MAKER = "fixture-leaf-maker"
TEST_SHA = "a" * 40
IMPL_SHA = "b" * 40
LATER_SHA = "c" * 40


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    return make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )


@dataclass(frozen=True)
class Views:
    projected: str
    hydrated: bool
    finished: bool


class Epic:
    """An epic with leaves, a maker seat that writes their records, and dependency edges."""

    def __init__(self, world: TeamWorld, title: str) -> None:
        self.world = world
        self.db = world.case.db
        self.tasks = LocalTaskManager(self.db)
        self.task = self.tasks.create_task(
            world.case.project_id,
            title,
            task_type="epic",
            created_in_session_id=world.case.creator,
        )
        self.maker = world.case.session("maker", parent=world.case.creator)
        grant_step_writes(
            self.db, self.maker, world.case.project_id, MAKER, ("test", "implementation")
        )

    def leaf(self, title: str) -> Task:
        return self.tasks.create_task(
            self.world.case.project_id,
            title,
            parent_task_id=self.task.id,
            category="code",
            created_in_session_id=self.world.case.creator,
            validation_criteria=f"{title} lands with an approved implementation record.",
        )

    def blocks(self, blocker: Task, blocked: Task) -> None:
        TaskDependencyManager(self.db).add_dependency(blocked.id, blocker.id, "blocks")

    def link(self, task: Task, *shas: str) -> None:
        self.db.execute(
            "UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps(list(shas)), task.id)
        )

    def record(
        self,
        task: Task,
        step: TaskStep,
        status: StepStatus,
        *,
        sha: str | None = None,
        disposition: str | None = None,
        round_number: int = 1,
    ) -> None:
        current = self.tasks.get_task(task.id)
        record_task_step(
            self.db,
            task_id=task.id,
            session_id=self.maker,
            step=step,
            submission=StepSubmission.model_validate(
                {
                    "status": status,
                    "epoch": current.review_epoch,
                    "round_number": round_number,
                    "feedback": f"{step} {status}",
                    "commit_sha": sha,
                    "disposition": disposition,
                }
            ),
        )

    def views(self, blocker: Task, blocked: Task) -> Views:
        row = self.db.fetchone(
            f"SELECT ({projected_task_state_sql('t')}) AS state FROM tasks t WHERE t.id = %s",  # nosec B608
            (blocked.id,),
        )
        assert row is not None
        hydrated = blocker.id in _fetch_active_blocker_map(self.db, [blocked.id]).get(
            blocked.id, set()
        )
        current = self.tasks.get_task(blocker.id)
        finished = approved_task_step(
            current, list_task_step_records(self.db, blocker.id), "implementation"
        )
        return Views(str(row["state"]), hydrated, finished is not None)


BLOCKED = Views("blocked", True, False)
UNBLOCKED = Views("open", False, True)


async def _admit(world: TeamWorld, epic: Epic) -> None:
    """Bind an active team to the epic, as the devteam runbook's admit step does."""
    await world.admit(world.child(epic.task), epic.task)


async def test_finished_blocker_unblocks_inside_team(world: TeamWorld) -> None:
    epic = Epic(world, "Parallel leaves feed one dependent")
    first, second, dependent = epic.leaf("First"), epic.leaf("Second"), epic.leaf("Dependent")
    epic.blocks(first, dependent)
    epic.blocks(second, dependent)
    await _admit(world, epic)

    # Test commits are registered commits too, but they never finish a leaf.
    epic.link(first, TEST_SHA)
    epic.record(first, "test", "approved", sha=TEST_SHA)
    assert epic.views(first, dependent) == BLOCKED

    epic.link(first, TEST_SHA, IMPL_SHA)
    epic.record(first, "implementation", "approved", sha=IMPL_SHA)
    # Ruling 4: every parallel blocker finishes before the dependent unblocks, so the
    # dependent stays blocked by second while first is no longer an active blocker.
    assert epic.views(first, dependent) == Views("blocked", False, True)
    assert epic.views(second, dependent) == BLOCKED

    epic.link(second, IMPL_SHA)
    epic.record(second, "implementation", "approved", sha=IMPL_SHA)
    assert epic.views(second, dependent) == UNBLOCKED

    # A newer linked commit makes the earlier approval stale, and the dependent blocks again.
    epic.link(first, TEST_SHA, IMPL_SHA, LATER_SHA)
    assert epic.views(first, dependent) == BLOCKED


async def test_predicate_parity_outside_teams(world: TeamWorld) -> None:
    teamless = Epic(world, "No team bound")
    blocker, blocked = teamless.leaf("Blocker"), teamless.leaf("Blocked")
    teamless.blocks(blocker, blocked)
    teamless.link(blocker, IMPL_SHA)
    teamless.record(blocker, "implementation", "approved", sha=IMPL_SHA)
    # Finished, but no active epic team: blocking is unchanged until the blocker closes.
    assert teamless.views(blocker, blocked) == Views("blocked", True, True)

    teamed = Epic(world, "Team bound")
    inside = teamed.leaf("Inside")
    # Admission picks epic mode only when the epic already holds an open leaf.
    await _admit(world, teamed)
    teamed.link(inside, IMPL_SHA)
    teamed.record(inside, "implementation", "approved", sha=IMPL_SHA)
    # Across epics the team's epic does not hold both tasks, so blocking is unchanged.
    teamed.blocks(inside, blocked)
    assert teamed.views(inside, blocked) == Views("blocked", True, True)
    across = teamed.leaf("Depends on another epic")
    teamed.blocks(blocker, across)
    assert teamed.views(blocker, across) == Views("blocked", True, True)

    # Inside the team both views flip together.
    dependent = teamed.leaf("Dependent inside")
    teamed.blocks(inside, dependent)
    assert teamed.views(inside, dependent) == UNBLOCKED


async def test_no_work_and_superseded_blockers(world: TeamWorld) -> None:
    epic = Epic(world, "No-work and superseded leaves")
    moot, docs = epic.leaf("Found moot"), epic.leaf("Docs after moot")
    superseded, dependent = epic.leaf("Approved then rejected"), epic.leaf("Waits on it")
    epic.blocks(moot, docs)
    epic.blocks(superseded, dependent)
    await _admit(world, epic)

    epic.record(moot, "implementation", "approved", disposition="moot")
    assert epic.views(moot, docs) == UNBLOCKED

    epic.link(superseded, IMPL_SHA)
    epic.record(superseded, "implementation", "approved", sha=IMPL_SHA)
    assert epic.views(superseded, dependent) == UNBLOCKED
    # The latest implementation record wins, even at the same commit.
    epic.record(superseded, "implementation", "rejected", sha=IMPL_SHA, round_number=2)
    assert epic.views(superseded, dependent) == BLOCKED
