"""Epic-bound teams: epic mode, epic names, overlap refusals and scoped leaf claims (addendum 8.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.agents.isolation_models import SpawnConfig, generate_branch_name
from gobby.agents.team_lifecycle import TeamAdmissionRefused, TeamBinding
from gobby.agents.team_teardown import reconcile_team_teardowns
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from gobby.worktrees.deletion import WorktreeDeletionResult
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import SLUG, TITLE, TeamWorld, git_in, make_team_world

pytestmark = pytest.mark.unit

LEAF_TITLE = "Wire the scroll handler"


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


def new_task(
    world: TeamWorld, title: str, *, parent: Task | None = None, epic: bool = False
) -> Task:
    """An unclaimed task, so no outside claim stands in the way of admission."""
    return LocalTaskManager(world.case.db).create_task(
        world.case.project_id,
        title,
        parent_task_id=None if parent is None else parent.id,
        created_in_session_id=world.case.creator,
        task_type="epic" if epic else "task",
        validation_criteria=None if epic else "The leaf works.",
    )


def team_record(world: TeamWorld, runbook: Any) -> dict[str, Any]:
    return cast(dict[str, Any], world.admit_step_output(runbook)["team"])


async def refused(world: TeamWorld, runbook: Any, task: Task) -> TeamAdmissionRefused:
    with pytest.raises(TeamAdmissionRefused) as refusal:
        await world.admit(runbook, task)
    return refusal.value


@pytest.mark.asyncio
async def test_epic_admission_names_and_mode(world: TeamWorld) -> None:
    epic = new_task(world, TITLE, epic=True)
    new_task(world, LEAF_TITLE, parent=epic)
    runbook = world.child(epic)

    team = await world.admit(runbook, epic)

    branch = f"epic/{epic.seq_num}/main"
    assert team.branch == branch
    assert Path(team.worktree_path).name == f"wt-epic-{epic.seq_num}-{SLUG}"
    assert git_in(team.worktree_path, "branch", "--show-current") == branch
    assert team_record(world, runbook)["mode"] == "epic"
    assert world.task_branches() == []

    # A title with no slug characters drops the slug part.
    bare = new_task(world, "!!! ???", epic=True)
    new_task(world, LEAF_TITLE, parent=bare)
    bare_team = await world.admit(world.child(bare), bare)
    assert Path(bare_team.worktree_path).name == f"wt-epic-{bare.seq_num}"
    assert bare_team.branch == f"epic/{bare.seq_num}/main"

    # A leaf under an unadmitted epic, and an epic with no open descendants, stay single-task.
    other = new_task(world, "Another epic", epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=other)
    leaf_run = world.child(leaf)
    leaf_team = await world.admit(leaf_run, leaf)
    assert leaf_team.branch == f"task-{leaf.seq_num}-wire-the-scroll-handler"
    assert team_record(world, leaf_run)["mode"] == "single"
    empty = new_task(world, "An empty epic", epic=True)
    empty_run = world.child(empty)
    empty_team = await world.admit(empty_run, empty)
    assert empty_team.branch == f"task-{empty.seq_num}-an-empty-epic"
    assert team_record(world, empty_run)["mode"] == "single"


def test_spawn_default_branch_name_is_unchanged() -> None:
    config = SpawnConfig(
        prompt="",
        task_id="task-id",
        task_title=TITLE,
        task_seq_num=7,
        branch_name=None,
        branch_prefix=None,
        base_branch="trunk",
        project_id="project-id",
        project_path="/tmp/project",
        provider="claude",
        parent_session_id="parent",
    )
    assert generate_branch_name(config) == f"task-7-{SLUG}"


@pytest.mark.asyncio
async def test_overlapping_admissions_refused(world: TeamWorld) -> None:
    # A descendant with its own team blocks admitting its epic.
    epic = new_task(world, TITLE, epic=True)
    phase = new_task(world, "Phase one", parent=epic, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=phase)
    leaf_team = await world.admit(world.child(leaf), leaf)
    refusal = await refused(world, world.child(epic), epic)
    assert refusal.code == "team_overlap"
    assert leaf.id in str(refusal)

    # An epic with an active team blocks admitting any descendant at any depth.
    second = new_task(world, "Second epic", epic=True)
    second_phase = new_task(world, "Phase two", parent=second, epic=True)
    deep_leaf = new_task(world, LEAF_TITLE, parent=second_phase)
    epic_team = await world.admit(world.child(second), second)
    for descendant in (second_phase, deep_leaf):
        refusal = await refused(world, world.child(descendant), descendant)
        assert refusal.code == "team_overlap"
        assert epic_team.team_id in str(refusal)

    # A descendant claimed by a session outside the admitting team blocks the epic.
    third = new_task(world, "Third epic", epic=True)
    claimed = new_task(world, LEAF_TITLE, parent=third)
    LocalTaskManager(world.case.db).claim_task(claimed.id, world.case.claimant, force=True)
    refusal = await refused(world, world.child(third), third)
    assert refusal.code == "team_descendant_claimed"
    assert claimed.id in str(refusal)

    # Refusals created nothing: only the two admitted teams own trees.
    assert sorted(world.tree_ids()) == sorted([leaf_team.worktree_id, epic_team.worktree_id])


@pytest.mark.asyncio
async def test_descendant_claims_scoped_to_team(world: TeamWorld) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    tools = create_task_registry(tasks)
    epic = new_task(world, TITLE, epic=True)
    phase = new_task(world, "Phase one", parent=epic, epic=True)
    leaf, deep = new_task(world, LEAF_TITLE, parent=epic), new_task(world, "Deep", parent=phase)
    runbook = world.child(epic)
    team = await world.admit(runbook, epic)
    team_seat = world.case.session("developer", parent=runbook.id)
    outsider = world.case.session("developer", parent=world.case.creator)

    async def claim(session_id: str, task: Task) -> dict[str, Any]:
        with session_context_for_test(session_id):
            return cast(dict[str, Any], await tools.call("claim_task", {"task_id": task.id}))

    for target in (leaf, deep):
        reply = await claim(outsider, target)
        assert reply.get("success") is not True, reply
        assert team.team_id in str(reply), reply
        assert tasks.get_task(target.id).claimed_by_session_id is None

        reply = await claim(team_seat, target)
        assert reply.get("success") is True, reply
        assert tasks.get_task(target.id).claimed_by_session_id == team_seat

    # Outside the epic, claiming is unchanged.
    unrelated = new_task(world, "Unrelated task")
    reply = await claim(outsider, unrelated)
    assert reply.get("success") is True, reply


@pytest.mark.asyncio
async def test_topology_and_bound_close_teardown(world: TeamWorld) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    epic = new_task(world, TITLE, epic=True)
    phase = new_task(world, "Phase one", parent=epic, epic=True)
    nested_leaf = new_task(world, LEAF_TITLE, parent=phase)
    root_leaf = new_task(world, "Root leaf", parent=epic)
    runbook = world.child(epic)
    team = await world.admit(runbook, epic)
    manager = world.case.session("dev-manager", parent=runbook.id)
    tasks.claim_task(epic.id, manager, force=True)

    deleted: list[TeamBinding] = []

    async def terminate(_run: AgentRun) -> bool:
        return True

    async def delete(binding: TeamBinding) -> WorktreeDeletionResult:
        deleted.append(binding)
        return WorktreeDeletionResult(success=True)

    async def teardown_pass() -> list[TeamBinding]:
        deleted.clear()
        later = datetime.now(UTC) + timedelta(minutes=5)
        await reconcile_team_teardowns(db, terminate=terminate, delete=delete, now=later)
        return list(deleted)

    # Closing a nested leaf cascades its phase epic closed; neither close tears the team down.
    tasks.close_task(nested_leaf.id, reason="done")
    assert tasks.get_task(phase.id).closed_at is not None
    assert await teardown_pass() == []
    # The manager's claim stops the cascade at the bound epic.
    tasks.close_task(root_leaf.id, reason="done")
    assert tasks.get_task(epic.id).closed_at is None
    assert await teardown_pass() == []
    # Closing the bound epic queues exactly one teardown.
    tasks.close_task(epic.id, reason="done")
    assert [binding.team_id for binding in await teardown_pass()] == [team.team_id]

    # A non-epic descendant with open children is neither a container nor a leaf.
    bad = new_task(world, "Malformed epic", epic=True)
    parent_leaf = new_task(world, "Parent leaf", parent=bad)
    new_task(world, "Child of a leaf", parent=parent_leaf)
    bad_run = world.child(bad)
    refusal = await refused(world, bad_run, bad)
    assert refusal.code == "team_epic_topology"
    assert parent_leaf.id in str(refusal)
    assert world.admit_step_output(bad_run) == {}
    assert world.tree_ids() == [team.worktree_id]
