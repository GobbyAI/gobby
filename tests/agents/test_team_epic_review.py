"""Explicit holds and parent authority survive removal of automatic review rounds."""

from __future__ import annotations

import pytest

from gobby.agents.team_admission_records import TeamAdmissionRefused
from gobby.agents.team_review import seat_team_held
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager
from tests.agents import test_team_epic_dispatch
from tests.agents.test_team_epic_dispatch import epic_team, new_task
from tests.fixtures.team_case import TeamWorld

pytestmark = pytest.mark.unit
world = test_team_epic_dispatch.world


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_epic_hold", [False, True])
async def test_admission_respects_only_explicit_parent_hold(
    world: TeamWorld, explicit_epic_hold: bool
) -> None:
    epic = new_task(world, "Held epic", epic=True)
    leaf = new_task(world, "Ready leaf", parent=epic)
    sibling = new_task(world, "Stuck sibling", parent=epic)
    team = await epic_team(world, epic)
    tasks = LocalTaskManager(world.case.db)
    tasks.escalate_task(epic.id if explicit_epic_hold else sibling.id, "Operator hold")
    run = world.child(leaf, parent=team.manager)

    assert seat_team_held(world.case.db, team.binding) is explicit_epic_hold
    if explicit_epic_hold:
        with pytest.raises(TeamAdmissionRefused) as refused:
            await world.admit(run, leaf, parent_team_id=team.binding.team_id)
        assert refused.value.code == "team_held"
        tasks.de_escalate_task(epic.id, reason="Operator released hold")

    pod = await world.admit(run, leaf, parent_team_id=team.binding.team_id)
    assert pod.parent_team_id == team.binding.team_id
    assert not seat_team_held(world.case.db, pod)


@pytest.mark.asyncio
async def test_running_leaf_team_observes_parent_hold_and_release(world: TeamWorld) -> None:
    epic = new_task(world, "Parent", epic=True)
    leaf = new_task(world, "Leaf", parent=epic)
    team = await epic_team(world, epic)
    pod = await world.admit(
        world.child(leaf, parent=team.manager), leaf, parent_team_id=team.binding.team_id
    )
    tasks = LocalTaskManager(world.case.db)

    tasks.escalate_task(epic.id, "Operator hold")
    assert seat_team_held(world.case.db, pod)

    tasks.de_escalate_task(epic.id, reason="Operator released hold")
    assert not seat_team_held(world.case.db, pod)


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_admission_waits_for_unresolved_dependency(world: TeamWorld, external: bool) -> None:
    epic = new_task(world, "Dependency ordered teams", epic=True)
    leaf = new_task(world, "Dependent leaf", parent=epic)
    prerequisite = new_task(world, "Prerequisite", parent=None if external else epic)
    TaskDependencyManager(world.case.db).add_dependency(leaf.id, prerequisite.id, "blocks")
    team = await epic_team(world, epic)
    run = world.child(leaf, parent=team.manager)

    with pytest.raises(TeamAdmissionRefused) as refused:
        await world.admit(run, leaf, parent_team_id=team.binding.team_id)
    assert refused.value.code == "pod_leaf_blocked"

    LocalTaskManager(world.case.db).close_task(prerequisite.id, reason="moot")
    pod = await world.admit(run, leaf, parent_team_id=team.binding.team_id)
    assert pod.parent_team_id == team.binding.team_id
    assert pod.worktree_id != team.binding.worktree_id


@pytest.mark.asyncio
async def test_unrelated_coordinator_cannot_admit_another_teams_leaf(world: TeamWorld) -> None:
    epic = new_task(world, "Parent", epic=True)
    leaf = new_task(world, "Leaf", parent=epic)
    team = await epic_team(world, epic)
    other = await epic_team(world, new_task(world, "Unrelated parent", epic=True))

    with pytest.raises(TeamAdmissionRefused) as refused:
        await world.admit(
            world.child(leaf, parent=other.manager), leaf, parent_team_id=team.binding.team_id
        )
    assert refused.value.code == "pod_parent_invalid"
