"""Claim coordination is declared authority; a spawned team manager stays within its team."""

from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import _claim_release, create_task_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskLifecycleEventManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_sessions import AgentSessions
from tests.fixtures.isolated_checkout import insert_isolated_machine
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TITLE, TeamWorld, make_team_world, synthetic_manager_body

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("operation", ["release_task_claim", "hand_off_task_claim"])
async def test_dirty_claim_refusal_preserves_owner_and_attribution_atomically(
    temp_db: HubDatabase, sample_git_project: dict[str, Any], operation: str
) -> None:
    sessions = AgentSessions(temp_db, sample_git_project["id"])
    coordinator = sessions.add("coordinator", capabilities=("manage_task_claims",))
    owner = sessions.add("worker")
    recipient = sessions.add("successor")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_git_project["id"], "Keep dirty work with its owner")
    manager.claim_task(task.id, owner)
    repo = Path(sample_git_project["repo_path"])
    (repo / "owned.py").write_text("value = 1\n")
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        owner,
        {
            "task_edited_files": {task.id: ["owned.py"]},
            "task_edited_file_checkouts": {task.id: {str(repo): ["owned.py"]}},
            "session_dirty_files": ["owned.py"],
            "session_dirty_file_checkouts": {str(repo): ["owned.py"]},
        },
    )
    before = variables.get_variables(owner)
    arguments = {
        "task_id": task.id,
        "holder_session_ref": owner,
        "reason": "Attempt explicit ownership coordination.",
    }
    if operation == "hand_off_task_claim":
        arguments["recipient_session_ref"] = recipient
    with session_context_for_test(coordinator):
        result = await create_task_registry(manager).call(operation, arguments)
    assert result["error_code"] == "task_has_uncommitted_changes", result
    assert manager.get_task(task.id).claimed_by_session_id == owner
    assert variables.get_variables(owner) == before
    assert (
        temp_db.fetchall(
            "SELECT id FROM task_lifecycle_events WHERE task_id = %s "
            "AND to_state = 'claim:handed_off'",
            (task.id,),
        )
        == []
    )


async def test_claim_handoff_needs_no_team_binding(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = AgentSessions(temp_db, sample_project["id"])
    coordinator = sessions.add("custom-coordinator", capabilities=("manage_task_claims",))
    holder = sessions.add("custom-worker")
    recipient = sessions.add("custom-successor")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Transfer ownership", validation_criteria="Claim moves atomically."
    )
    manager.claim_task(task.id, holder)
    registry = create_task_registry(manager)

    with session_context_for_test(coordinator):
        result = await registry.call(
            "hand_off_task_claim",
            {
                "task_id": task.id,
                "holder_session_ref": holder,
                "recipient_session_ref": recipient,
                "reason": "Continue the same task in a different seat.",
            },
        )

    assert result.get("success") is True, result
    assert manager.get_task(task.id).claimed_by_session_id == recipient


@pytest.mark.parametrize("operation", ["release_task_claim", "hand_off_task_claim"])
async def test_claim_management_respects_the_declared_task_scope(
    temp_db: HubDatabase, sample_project: dict[str, Any], operation: str
) -> None:
    sessions = AgentSessions(temp_db, sample_project["id"])
    coordinator = sessions.add(
        "scoped-coordinator",
        capabilities=("manage_task_claims",),
        definition_fields={"task_claim_scope": {"categories": ["docs"]}},
    )
    holder = sessions.add("worker")
    recipient = sessions.add("successor")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Code task", category="code", validation_criteria="Preserve claim."
    )
    manager.claim_task(task.id, holder)
    arguments = {"task_id": task.id, "holder_session_ref": holder, "reason": "Scope check."}
    if operation == "hand_off_task_claim":
        arguments["recipient_session_ref"] = recipient

    with session_context_for_test(coordinator):
        result = await create_task_registry(manager).call(operation, arguments)

    assert "error" in result
    assert manager.get_task(task.id).claimed_by_session_id == holder


@pytest.mark.parametrize("operation", ["release_task_claim", "hand_off_task_claim"])
async def test_claim_scope_is_rechecked_after_ownership_changes(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    sessions = AgentSessions(temp_db, sample_project["id"])
    coordinator = sessions.add(
        "own-claim-coordinator",
        capabilities=("manage_task_claims",),
        definition_fields={"task_claim_scope": {"own_claim_only": True}},
    )
    holder = sessions.add("next-owner")
    recipient = sessions.add("subsequent-owner")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Changing ownership",
        validation_criteria="Do not release a foreign claim.",
    )
    manager.claim_task(task.id, coordinator)
    original = _claim_release._may_manage_claim
    changed = False

    def change_owner_after_admission(ctx: RegistryContext, caller: Session, task_id: str) -> bool:
        nonlocal changed
        allowed = original(ctx, caller, task_id)
        if not changed:
            changed = True
            manager.claim_task(task_id, holder, force=True)
        return allowed

    monkeypatch.setattr(_claim_release, "_may_manage_claim", change_owner_after_admission)
    arguments = {"task_id": task.id, "holder_session_ref": holder, "reason": "Race check."}
    if operation == "hand_off_task_claim":
        arguments["recipient_session_ref"] = recipient
    with session_context_for_test(coordinator):
        result = await create_task_registry(manager).call(operation, arguments)

    assert changed
    assert "error" in result
    assert manager.get_task(task.id).claimed_by_session_id == holder


@pytest.mark.parametrize("operation", ["release_task_claim", "hand_off_task_claim"])
async def test_remote_cohort_cannot_use_local_dirty_file_checks(
    temp_db: HubDatabase, sample_project: dict[str, Any], operation: str
) -> None:
    sessions = AgentSessions(temp_db, sample_project["id"])
    coordinator = sessions.add("remote-coordinator", capabilities=("manage_task_claims",))
    holder = sessions.add("remote-worker")
    recipient = sessions.add("remote-successor")
    machine = insert_isolated_machine(temp_db)
    for session_id in (coordinator, holder, recipient):
        temp_db.execute("UPDATE sessions SET machine_id = %s WHERE id = %s", (machine, session_id))
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Remote work", validation_criteria="Preserve the remote claim."
    )
    manager.claim_task(task.id, holder)
    arguments = {"task_id": task.id, "holder_session_ref": holder, "reason": "Remote check."}
    if operation == "hand_off_task_claim":
        arguments["recipient_session_ref"] = recipient

    with session_context_for_test(coordinator):
        result = await create_task_registry(manager).call(operation, arguments)

    assert "error" in result
    assert manager.get_task(task.id).claimed_by_session_id == holder


# ---------------------------------------------------------------------------
# A spawned team manager coordinates claims only within its own team.
# ---------------------------------------------------------------------------

MANAGER = "claim-team-lead"
LEAF_TITLE = "Wire the claim handler"


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


def _task(world: TeamWorld, title: str, *, parent: Task | None = None, epic: bool = False) -> Task:
    return LocalTaskManager(world.case.db).create_task(
        world.case.project_id,
        title,
        parent_task_id=None if parent is None else parent.id,
        created_in_session_id=world.case.creator,
        task_type="epic" if epic else "task",
        validation_criteria=None if epic else "The claim moves.",
    )


def _spawned_manager(world: TeamWorld, runbook: Session, task: Task, name: str = MANAGER) -> str:
    """A spawned seat of ``runbook`` that declares claim management, running bound to ``task``."""
    db = world.case.db
    if AgentDefinitionManager(db).get_by_name(name, world.case.project_id) is None:
        body = synthetic_manager_body()
        body["name"] = name
        AgentDefinitionManager(db).create(name, body, project_id=world.case.project_id)
    manager = world.case.session(name, parent=runbook.id)
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent_session_id=runbook.id,
        provider="claude",
        prompt="Coordinate this team's claims.",
        agent_name=name,
        child_session_id=manager,
        task_id=task.id,
    )
    started = runs.start(run.id)
    assert started is not None and started.status == "running"
    SessionVariableManager(db).merge_variables(manager, {"_agent_type": name})
    seat = SessionManager(db).update_terminal_pickup_metadata(manager, agent_run_id=run.id)
    assert seat is not None and seat.agent_run_id == run.id
    return manager


async def _call(
    tasks: LocalTaskManager, session_id: str, operation: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    with session_context_for_test(session_id):
        result: dict[str, Any] = await create_task_registry(tasks).call(operation, arguments)
    return result


@pytest.mark.asyncio
async def test_team_manager_hands_off_a_claim_between_its_own_seats(world: TeamWorld) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    task = _task(world, TITLE)
    runbook = world.child(task)
    await world.admit(runbook, task)
    manager = _spawned_manager(world, runbook, task)
    holder = world.case.session("fixture-worker", parent=runbook.id)
    recipient = world.case.session("fixture-definition-c02389e4", parent=runbook.id)
    tasks.claim_task(task.id, holder)

    result = await _call(
        tasks,
        manager,
        "hand_off_task_claim",
        {
            "task_id": task.id,
            "holder_session_ref": holder,
            "recipient_session_ref": recipient,
            "reason": "Re-seat within the team.",
        },
    )

    assert result.get("success") is True, result
    assert tasks.get_task(task.id).claimed_by_session_id == recipient
    handoffs = [
        event
        for event in TaskLifecycleEventManager(db).list_lifecycle_events(task.id)
        if event.to_state == "claim:handed_off"
    ]
    assert handoffs and handoffs[-1].by_actor == manager


@pytest.mark.asyncio
async def test_team_manager_hands_off_a_claim_after_its_tree_lands(world: TeamWorld) -> None:
    """A landing marks the team's tree merged while the open task still belongs to that team."""
    db = world.case.db
    tasks = LocalTaskManager(db)
    task = _task(world, TITLE)
    runbook = world.child(task)
    binding = await world.admit(runbook, task)
    manager = _spawned_manager(world, runbook, task)
    holder = world.case.session("fixture-worker", parent=runbook.id)
    recipient = world.case.session("fixture-definition-c02389e4", parent=runbook.id)
    tasks.claim_task(task.id, holder)
    assert binding.worktree_id is not None
    LocalWorktreeManager(db).mark_merged(binding.worktree_id)

    result = await _call(
        tasks,
        manager,
        "hand_off_task_claim",
        {
            "task_id": task.id,
            "holder_session_ref": holder,
            "recipient_session_ref": recipient,
            "reason": "Re-seat after the team's tree landed.",
        },
    )

    assert result.get("success") is True, result
    assert tasks.get_task(task.id).claimed_by_session_id == recipient


@pytest.mark.asyncio
async def test_relaunched_team_manager_hands_off_a_claim_after_its_tree_lands(
    world: TeamWorld,
) -> None:
    """A manager relaunched by team_id joins its team's binding and keeps authority after landing."""
    db = world.case.db
    tasks = LocalTaskManager(db)
    task = _task(world, TITLE)
    runbook = world.child(task)
    binding = await world.admit(runbook, task)
    relaunch = world.child(task)
    await world.admit(relaunch, task, team_id=binding.team_id)
    manager = _spawned_manager(world, relaunch, task)
    holder = world.case.session("fixture-worker", parent=runbook.id)
    recipient = world.case.session("fixture-definition-c02389e4", parent=runbook.id)
    tasks.claim_task(task.id, holder)
    assert binding.worktree_id is not None
    LocalWorktreeManager(db).mark_merged(binding.worktree_id)

    result = await _call(
        tasks,
        manager,
        "hand_off_task_claim",
        {
            "task_id": task.id,
            "holder_session_ref": holder,
            "recipient_session_ref": recipient,
            "reason": "Re-seat after the relaunched team's tree landed.",
        },
    )

    assert result.get("success") is True, result
    assert tasks.get_task(task.id).claimed_by_session_id == recipient


@pytest.mark.asyncio
async def test_team_manager_releases_a_claim_within_its_own_team(world: TeamWorld) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    task = _task(world, TITLE)
    runbook = world.child(task)
    await world.admit(runbook, task)
    manager = _spawned_manager(world, runbook, task)
    holder = world.case.session("fixture-worker", parent=runbook.id)
    tasks.claim_task(task.id, holder)

    result = await _call(
        tasks,
        manager,
        "release_task_claim",
        {"task_id": task.id, "holder_session_ref": holder, "reason": "Seat is stuck."},
    )

    assert result.get("success") is True, result
    assert tasks.get_task(task.id).claimed_by_session_id is None


@pytest.mark.asyncio
async def test_epic_manager_hands_off_a_leaf_claim_within_the_leaf_team(world: TeamWorld) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    epic = _task(world, TITLE, epic=True)
    leaf = _task(world, LEAF_TITLE, parent=epic)
    epic_runbook = world.child(epic)
    epic_binding = await world.admit(epic_runbook, epic)
    manager = _spawned_manager(world, epic_runbook, epic)
    leaf_runbook = world.child(leaf, parent=manager)
    await world.admit(leaf_runbook, leaf, parent_team_id=epic_binding.team_id)
    holder = world.case.session("fixture-worker", parent=leaf_runbook.id)
    recipient = world.case.session("fixture-definition-c02389e4", parent=leaf_runbook.id)
    tasks.claim_task(leaf.id, holder)

    result = await _call(
        tasks,
        manager,
        "hand_off_task_claim",
        {
            "task_id": leaf.id,
            "holder_session_ref": holder,
            "recipient_session_ref": recipient,
            "reason": "Re-seat within the leaf team.",
        },
    )

    assert result.get("success") is True, result
    assert tasks.get_task(leaf.id).claimed_by_session_id == recipient


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["hand_off_task_claim", "release_task_claim"])
async def test_team_manager_cannot_move_a_claim_on_another_teams_task(
    world: TeamWorld, operation: str
) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    own = _task(world, TITLE)
    own_runbook = world.child(own)
    await world.admit(own_runbook, own)
    manager = _spawned_manager(world, own_runbook, own)
    other = _task(world, "Another team's task")
    other_runbook = world.child(other)
    await world.admit(other_runbook, other)
    holder = world.case.session("fixture-worker", parent=other_runbook.id)
    recipient = world.case.session("fixture-definition-c02389e4", parent=own_runbook.id)
    tasks.claim_task(other.id, holder)
    arguments = {"task_id": other.id, "holder_session_ref": holder, "reason": "Cross-team."}
    if operation == "hand_off_task_claim":
        arguments["recipient_session_ref"] = recipient

    result = await _call(tasks, manager, operation, arguments)

    assert result.get("error_code") == "team_manager_required", result
    assert tasks.get_task(other.id).claimed_by_session_id == holder


@pytest.mark.asyncio
@pytest.mark.parametrize("outsider", ["other_team_seat", "unspawned_session"])
async def test_team_manager_refuses_a_recipient_outside_its_team(
    world: TeamWorld, outsider: str
) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    own = _task(world, TITLE)
    own_runbook = world.child(own)
    await world.admit(own_runbook, own)
    manager = _spawned_manager(world, own_runbook, own)
    holder = world.case.session("fixture-worker", parent=own_runbook.id)
    tasks.claim_task(own.id, holder)
    if outsider == "other_team_seat":
        other = _task(world, "Another team's task")
        other_runbook = world.child(other)
        await world.admit(other_runbook, other)
        recipient = world.case.session("fixture-definition-c02389e4", parent=other_runbook.id)
    else:
        recipient = AgentSessions(db, world.case.project_id).add("unspawned-successor")

    result = await _call(
        tasks,
        manager,
        "hand_off_task_claim",
        {
            "task_id": own.id,
            "holder_session_ref": holder,
            "recipient_session_ref": recipient,
            "reason": "Outside recipient.",
        },
    )

    assert result.get("error_code") == "team_seat_required", result
    assert tasks.get_task(own.id).claimed_by_session_id == holder


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["hand_off_task_claim", "release_task_claim"])
async def test_team_manager_refuses_a_holder_outside_its_team(
    world: TeamWorld, operation: str
) -> None:
    db = world.case.db
    tasks = LocalTaskManager(db)
    own = _task(world, TITLE)
    own_runbook = world.child(own)
    await world.admit(own_runbook, own)
    manager = _spawned_manager(world, own_runbook, own)
    outsider = AgentSessions(db, world.case.project_id).add("unspawned-holder")
    tasks.claim_task(own.id, outsider)
    arguments = {"task_id": own.id, "holder_session_ref": outsider, "reason": "Outside holder."}
    if operation == "hand_off_task_claim":
        arguments["recipient_session_ref"] = world.case.session(
            "fixture-worker", parent=own_runbook.id
        )

    result = await _call(tasks, manager, operation, arguments)

    assert result.get("error_code") == "team_seat_required", result
    assert tasks.get_task(own.id).claimed_by_session_id == outsider


@pytest.mark.asyncio
async def test_unspawned_root_moves_a_claim_on_any_teams_task(world: TeamWorld) -> None:
    """An unspawned local root keeps project-wide claim authority."""
    db = world.case.db
    tasks = LocalTaskManager(db)
    root = AgentSessions(db, world.case.project_id).add(
        "root-claim-coordinator", capabilities=("manage_task_claims",)
    )
    other = _task(world, TITLE)
    other_runbook = world.child(other)
    await world.admit(other_runbook, other)
    holder = world.case.session("fixture-worker", parent=other_runbook.id)
    recipient = world.case.session("fixture-definition-c02389e4", parent=other_runbook.id)
    tasks.claim_task(other.id, holder)

    result = await _call(
        tasks,
        root,
        "hand_off_task_claim",
        {
            "task_id": other.id,
            "holder_session_ref": holder,
            "recipient_session_ref": recipient,
            "reason": "Operator re-seat.",
        },
    )

    assert result.get("success") is True, result
    assert tasks.get_task(other.id).claimed_by_session_id == recipient
