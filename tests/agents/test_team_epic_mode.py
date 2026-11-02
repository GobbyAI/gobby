"""Epic-bound teams: epic mode, epic names, overlap refusals and scoped leaf claims (addendum 8.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.agents.isolation_models import SpawnConfig, generate_branch_name
from gobby.agents.team_admission_records import TeamAdmissionRefused, TeamBinding
from gobby.agents.team_lifecycle import seat_team
from gobby.agents.team_teardown import reconcile_team_teardowns
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.agents import AgentRun
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from gobby.worktrees.deletion import WorktreeDeletionResult
from tests.fixtures.agent_sessions import AgentSessions
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


def tree_of(binding: TeamBinding) -> Path:
    """The team tree a binding names; these tests always admit a team with one."""
    assert binding.worktree_path is not None
    return Path(binding.worktree_path)


def tree_id_of(binding: TeamBinding) -> str:
    """The team tree id a binding names; these tests always admit a team with one."""
    assert binding.worktree_id is not None
    return binding.worktree_id


def new_task(
    world: TeamWorld,
    title: str,
    *,
    parent: Task | None = None,
    epic: bool = False,
    category: str | None = None,
) -> Task:
    """An unclaimed task, so no outside claim stands in the way of admission."""
    return LocalTaskManager(world.case.db).create_task(
        world.case.project_id,
        title,
        parent_task_id=None if parent is None else parent.id,
        created_in_session_id=world.case.creator,
        task_type="epic" if epic else "task",
        category=category,
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
    assert tree_of(team).name == f"wt-epic-{epic.seq_num}-{SLUG}"
    assert git_in(tree_of(team), "branch", "--show-current") == branch
    assert team_record(world, runbook)["mode"] == "epic"
    assert world.task_branches() == []

    # A title with no slug characters drops the slug part.
    bare = new_task(world, "!!! ???", epic=True)
    new_task(world, LEAF_TITLE, parent=bare)
    bare_team = await world.admit(world.child(bare), bare)
    assert tree_of(bare_team).name == f"wt-epic-{bare.seq_num}"
    assert bare_team.branch == f"epic/{bare.seq_num}/main"

    # A leaf under an unadmitted epic stays single-task; empty epics retain epic identity.
    other = new_task(world, "Another epic", epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=other)
    leaf_run = world.child(leaf)
    leaf_team = await world.admit(leaf_run, leaf)
    assert leaf_team.branch == f"task-{leaf.seq_num}-wire-the-scroll-handler"
    assert team_record(world, leaf_run)["mode"] == "single"
    empty = new_task(world, "An empty epic", epic=True)
    empty_run = world.child(empty)
    empty_team = await world.admit(empty_run, empty)
    assert empty_team.branch == f"epic/{empty.seq_num}/main"
    assert team_record(world, empty_run)["mode"] == "epic"


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

    # A foreign claim remains untouched while the epic admits ready sibling work.
    third = new_task(world, "Third epic", epic=True)
    claimed = new_task(world, LEAF_TITLE, parent=third)
    LocalTaskManager(world.case.db).claim_task(claimed.id, world.case.claimant, force=True)
    third_team = await world.admit(world.child(third), third)
    assert (
        LocalTaskManager(world.case.db).get_task(claimed.id).claimed_by_session_id
        == world.case.claimant
    )

    assert sorted(world.tree_ids()) == sorted(
        [tree_id_of(leaf_team), tree_id_of(epic_team), tree_id_of(third_team)]
    )


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
async def test_team_seat_cannot_claim_its_epic_root(world: TeamWorld) -> None:
    """An epic root has no acceptance criteria, so its team's seats claim only its open leaves."""
    db = world.case.db
    tasks = LocalTaskManager(db)
    tools = create_task_registry(tasks)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    runbook = world.child(epic)
    await world.admit(runbook, epic)
    seat = world.case.session("test-writer", parent=runbook.id)

    async def claim(task: Task) -> dict[str, Any]:
        with session_context_for_test(seat):
            return cast(dict[str, Any], await tools.call("claim_task", {"task_id": task.id}))

    refused = await claim(epic)
    assert refused.get("success") is not True, refused
    assert refused.get("error_code") == "TASK_CLAIM_CONFLICT", refused
    assert tasks.get_task(epic.id).claimed_by_session_id is None

    reply = await claim(leaf)
    assert reply.get("success") is True, reply
    assert tasks.get_task(leaf.id).claimed_by_session_id == seat


@pytest.mark.asyncio
async def test_team_seat_claims_its_epic_root_once_every_descendant_closes(
    world: TeamWorld,
) -> None:
    """The epic root becomes the final validation only after its last open descendant closes."""
    db = world.case.db
    tasks = LocalTaskManager(db)
    tools = create_task_registry(tasks)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    runbook = world.child(epic)
    await world.admit(runbook, epic)
    seat = world.case.session("test-writer", parent=runbook.id)
    # Selected policy reserves the bound root for explicit completion, as epic closure does.
    owner = AgentSessions(db, world.case.project_id).add(
        "bound-root-owner",
        definition_fields={"workflows": {"rule_selectors": {"include": ["tag:root-close"]}}},
    )
    RuleDefinitionManager(db).create(
        "explicit-bound-root-close",
        {
            "event": "task_close",
            "when": f"task['id'] == '{epic.id}' and facts.get('closure_intent') == 'ancestor'",
            "effects": [{"type": "block", "reason": "Close the bound root explicitly."}],
        },
        project_id=world.case.project_id,
        tags=["root-close"],
    )

    async def claim() -> dict[str, Any]:
        with session_context_for_test(seat):
            return cast(dict[str, Any], await tools.call("claim_task", {"task_id": epic.id}))

    refused = await claim()
    assert refused.get("error_code") == "TASK_CLAIM_CONFLICT", refused
    assert "claim one of its open leaves" in str(refused.get("error")), refused

    tasks.close_task(leaf.id, reason="done", closed_in_session_id=owner)
    assert tasks.get_task(epic.id).closed_at is None
    reply = await claim()
    assert reply.get("success") is True, reply
    assert tasks.get_task(epic.id).claimed_by_session_id == seat


@pytest.mark.asyncio
async def test_planning_child_of_epic_is_claimable_by_non_team_session(world: TeamWorld) -> None:
    """A planning-category child under a live epic team is claimable by a plan writer seat."""
    tasks = LocalTaskManager(world.case.db)
    tools = create_task_registry(tasks)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    await world.admit(world.child(epic), epic)
    # The runbook creates the planning child after the team is live, under the team's leaf.
    planning = new_task(world, "Plan the leaf", parent=leaf, category="planning")
    writer = world.case.session("plan-writer", parent=world.case.creator)

    async def claim(session_id: str, task: Task) -> dict[str, Any]:
        with session_context_for_test(session_id):
            return cast(dict[str, Any], await tools.call("claim_task", {"task_id": task.id}))

    reply = await claim(writer, planning)
    assert reply.get("success") is True, reply
    assert tasks.get_task(planning.id).claimed_by_session_id == writer

    # Code leaves under the same epic keep the team-only claim scope.
    reply = await claim(writer, leaf)
    assert reply.get("success") is not True, reply
    assert tasks.get_task(leaf.id).claimed_by_session_id is None


@pytest.mark.asyncio
async def test_dispatched_seat_resolves_its_team_and_claims_its_leaf(world: TeamWorld) -> None:
    """A seat the Dev Team Lead spawns sits under that manager, and the manager under the runbook."""
    db = world.case.db
    tasks = LocalTaskManager(db)
    tools = create_task_registry(tasks)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    runbook = world.child(epic)
    team = await world.admit(runbook, epic)
    manager = world.case.session("dev-team-lead", parent=runbook.id)
    seat = world.case.session("developer", parent=manager)

    binding = seat_team(db, seat)
    assert binding is not None and binding.team_id == team.team_id

    with session_context_for_test(seat):
        reply = cast(dict[str, Any], await tools.call("claim_task", {"task_id": leaf.id}))
    assert reply.get("success") is True, reply
    assert tasks.get_task(leaf.id).claimed_by_session_id == seat


@pytest.mark.asyncio
async def test_authorized_coordinator_releases_an_epic_leaf_claim(world: TeamWorld) -> None:
    """A declared claim manager needs no team-manager binding."""
    db = world.case.db
    tasks = LocalTaskManager(db)
    tools = create_task_registry(tasks)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    runbook = world.child(epic)
    await world.admit(runbook, epic)
    manager = AgentSessions(db, world.case.project_id).add(
        "claim-coordinator", capabilities=("manage_task_claims",)
    )
    holder = world.case.session("test-writer", parent=runbook.id)
    with session_context_for_test(holder):
        claimed = cast(dict[str, Any], await tools.call("claim_task", {"task_id": leaf.id}))
    assert claimed.get("success") is True, claimed

    with session_context_for_test(manager):
        released = cast(
            dict[str, Any],
            await tools.call(
                "release_task_claim",
                {"task_id": leaf.id, "holder_session_ref": holder, "reason": "Seat is stuck"},
            ),
        )
    assert released.get("success") is True, released
    assert released["released_by_session_id"] == manager
    assert tasks.get_task(leaf.id).claimed_by_session_id is None


@pytest.mark.asyncio
async def test_scoped_coordinator_cannot_release_an_excluded_epic_leaf_claim(
    world: TeamWorld,
) -> None:
    """Team presence cannot override a definition's declared claim scope."""
    db = world.case.db
    tasks = LocalTaskManager(db)
    tools = create_task_registry(tasks)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    runbook = world.child(epic)
    await world.admit(runbook, epic)
    holder = world.case.session("test-writer", parent=runbook.id)
    tasks.claim_task(leaf.id, holder, force=True)

    other_epic = new_task(world, "Another team's epic", epic=True)
    other_runbook = world.child(other_epic)
    await world.admit(other_runbook, other_epic)
    other_manager = AgentSessions(db, world.case.project_id).add(
        "docs-claim-coordinator",
        capabilities=("manage_task_claims",),
        definition_fields={"task_claim_scope": {"categories": ["docs"]}},
    )

    with session_context_for_test(other_manager):
        refused = cast(
            dict[str, Any],
            await tools.call(
                "release_task_claim",
                {"task_id": leaf.id, "holder_session_ref": holder, "reason": "Seat is stuck"},
            ),
        )
    assert refused.get("error_code") == "coordinator_required", refused
    assert tasks.get_task(leaf.id).claimed_by_session_id == holder


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

    owner = AgentSessions(db, world.case.project_id).add(
        "bound-root-owner",
        definition_fields={"workflows": {"rule_selectors": {"include": ["tag:root-close"]}}},
    )
    RuleDefinitionManager(db).create(
        "explicit-bound-root-close",
        {
            "event": "task_close",
            "when": f"task['id'] == '{epic.id}' and facts.get('closure_intent') == 'ancestor'",
            "effects": [{"type": "block", "reason": "Close the bound root explicitly."}],
        },
        project_id=world.case.project_id,
        tags=["root-close"],
    )

    deleted: list[TeamBinding] = []

    async def terminate(_run: AgentRun, _closed_reason: str | None) -> bool:
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
    tasks.close_task(nested_leaf.id, reason="done", closed_in_session_id=owner)
    assert tasks.get_task(phase.id).closed_at is not None
    assert await teardown_pass() == []
    # Selected policy reserves the bound root for explicit completion.
    tasks.close_task(root_leaf.id, reason="done", closed_in_session_id=owner)
    assert tasks.get_task(epic.id).closed_at is None
    assert await teardown_pass() == []
    # Closing the bound epic queues exactly one teardown.
    tasks.close_task(epic.id, reason="done", closed_in_session_id=owner)
    assert [binding.team_id for binding in await teardown_pass()] == [team.team_id]

    # Non-epic containers are valid traversal nodes, with implementation leaves beneath them.
    bad = new_task(world, "Malformed epic", epic=True)
    parent_leaf = new_task(world, "Parent leaf", parent=bad)
    new_task(world, "Child of a leaf", parent=parent_leaf)
    bad_run = world.child(bad)
    nested_team = await world.admit(bad_run, bad)
    assert team_record(world, bad_run)["mode"] == "epic"
    assert sorted(world.tree_ids()) == sorted([tree_id_of(team), tree_id_of(nested_team)])
