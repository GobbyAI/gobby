"""A closed task's team collapses: its seats end, then its tree is deleted safely (plan 3.3)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml

import gobby.runner_lifecycle_agents as lifecycle_agents
import gobby.runner_lifecycle_callbacks as lifecycle_callbacks
from gobby.agents import resume_executor
from gobby.agents.resume_executor import ResumeAgentResult
from gobby.agents.team_lifecycle import (
    SeatTarget,
    TeamAdmissionRefused,
    TeamBinding,
    check_team_spawn,
)
from gobby.agents.team_teardown import FINDING_LABEL, TEARDOWN_GRACE
from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.tools import agent_cancellation
from gobby.runner_maintenance.isolation import team_tree_deleter
from gobby.sessions.handoff_records import (
    build_handoff_payload,
    get_agent_end_handoff,
    stage_agent_end_handoff,
)
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.daemon_resume_keys import CONSUMED_BY_KEY
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.definitions import PipelineDefinition
from gobby.worktrees.deletion import WorktreeDeletionResult
from tests.config_runtime_helpers import static_runtime_capture
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.postgres import TEST_USER_ID
from tests.fixtures.team_case import TeamWorld, execution_id, git_in, make_team_world, running_seat
from tests.fixtures.team_teardown_case import (
    Team,
    Terminations,
    admitted_team,
    branch_exists,
    closed_at,
    daemon,
    dispatched_seat,
    seat,
    session_status,
    teardown,
    teardown_pass,
)

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

pytestmark = pytest.mark.unit


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


def commit(team: Team, name: str) -> str:
    tree = tree_of(team.binding)
    (tree / name).write_text(f"# {name}\n")
    git_in(tree, "add", name)
    git_in(tree, "commit", "-m", f"Add {name}")
    return git_in(tree, "rev-parse", "HEAD")


def land(world: TeamWorld, team: Team, name: str) -> str:
    """Commit ``name`` in the team tree and merge its branch into trunk, as landing does."""
    sha = commit(team, name)
    world.case.git("merge", "--no-edit", team.binding.branch)
    return sha


def close(world: TeamWorld, team: Team, sha: str, *, minutes_ago: int = 0) -> Task:
    """The Dev Manager's committed close, optionally backdated past the teardown grace."""
    db = world.case.db
    db.execute("UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([sha]), team.task.id))
    LocalTaskManager(db).close_task(
        team.task.id, reason="done", closed_in_session_id=team.seats[0], closed_commit_sha=sha
    )
    db.execute(
        "UPDATE tasks SET closed_at = closed_at - make_interval(mins => %s) WHERE id = %s",
        (minutes_ago, team.task.id),
    )
    return LocalTaskManager(db).get_task(team.task.id)


def finding_tasks(world: TeamWorld, team: Team) -> list[Task]:
    return LocalTaskManager(world.case.db).list_tasks(
        project_id=world.case.project_id, label=f"team-{team.binding.team_id}"
    )


def seat_run(db: HubDatabase, team: Team, index: int) -> AgentRun:
    """The pending run the runbook launched ``team.seats[index]`` with."""
    (run,) = [
        run
        for run in LocalAgentRunManager(db).list_by_parent(team.runbook.id)
        if run.child_session_id == team.seats[index]
    ]
    return run


def park(db: HubDatabase, team: Team, index: int) -> AgentRun:
    """A daemon stop parked ``team.seats[index]``: run cancelled, process gone, session paused."""
    session = team.seats[index]
    runs = LocalAgentRunManager(db)
    run = seat_run(db, team, index)
    SessionManager(db).update_terminal_pickup_metadata(session, agent_run_id=run.id)
    assert runs.start(run.id) is not None
    runs.merge_resume_metadata(run.id, {"provider_native_session_id": f"native-{index}"})
    parked = runs.cancel(run.id, terminal_reason="daemon_stop")
    assert parked is not None and (parked.status, parked.pid) == ("cancelled", None)
    assert session_status(db, session) == "paused"
    return parked


def recovering(db: HubDatabase) -> GobbyRunner:
    """The daemon pieces the parked-seat resume retry uses."""
    return cast(
        "GobbyRunner",
        SimpleNamespace(
            agent_runner=SimpleNamespace(run_storage=LocalAgentRunManager(db)),
            database=db,
            db_executor=None,
            session_manager=SessionManager(db),
            config_runtime=SimpleNamespace(capture=static_runtime_capture(DaemonConfig())),
            completion_registry=None,
            websocket_server=None,
        ),
    )


async def test_close_collapses_all_team_seats(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    team = await admitted_team(world, "Collapse the closed team")
    # A replacement execution joined the team and launched its own reviewer.
    rejoined = world.child(team.task)
    await world.admit(rejoined, team.task, team.binding.team_id)
    world.finish(rejoined)
    seats = [*team.seats, seat(world, rejoined, team.binding, "code-reviewer")]
    bystander = await admitted_team(world, "Open team keeps working")
    sha = land(world, team, "collapse.py")
    closed = close(world, team, sha)
    runner = daemon(db)

    # The closing manager receives its close response before any seat ends.
    await teardown_pass(db, runner, closed_at(closed))
    assert terminations.calls == []

    await teardown_pass(db, runner, closed_at(closed) + TEARDOWN_GRACE)

    assert terminations.ended() == sorted(seats)
    assert all(get_agent_end_handoff(db, call["run"].id) for call in terminations.calls)
    assert {(call["effective_status"], call["debug"]) for call in terminations.calls} == {
        ("cancelled", False)
    }, "each seat's tab closes with its run"
    assert {session_status(db, session) for session in seats} == {"expired"}
    assert {session_status(db, session) for session in bystander.seats} == {"active"}
    assert tree_of(bystander.binding).is_dir()
    after = LocalTaskManager(db).get_task(team.task.id)
    assert (after.closed_at, after.commits, after.closed_commit_sha) == (
        closed.closed_at,
        [sha],
        sha,
    )
    assert world.case.git("merge-base", "--is-ancestor", sha, "trunk") == ""
    assert teardown(world, team) == {"outcome": "cleaned"}
    assert not tree_of(team.binding).exists()
    assert LocalWorktreeManager(db).get(tree_id_of(team.binding)) is None
    assert not branch_exists(world, team.binding.branch)
    with pytest.raises(TeamAdmissionRefused) as refused:
        check_team_spawn(db, team.runbook, SeatTarget(project_path=team.binding.worktree_path))
    assert refused.value.code == "team_closed"


async def test_teardown_recovers_after_restart(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    for step in ("_rotate_due_managed_credentials", "_reclassify_reconciliation_pending_runs"):
        monkeypatch.setattr(lifecycle_callbacks, step, AsyncMock(return_value=0))
    team = await admitted_team(world, "Restarted teardown team")
    refused = await admitted_team(world, "Restarted refusal team")
    close(world, team, land(world, team, "restart.py"), minutes_ago=1)

    # A daemon still starting leaves the closed team alone until its runs are reconciled.
    await lifecycle_callbacks.reconcile_agent_lifecycle_state(daemon(db, initialized=False))
    assert terminations.calls == []

    # Shutdown cancels the pass after one seat ended.
    terminations.stop_after = 1
    runner = daemon(db)
    with pytest.raises(asyncio.CancelledError):
        await lifecycle_callbacks.reconcile_agent_lifecycle_state(runner)
    assert len(terminations.calls) == 1

    # The next start ends the remaining seats, then stops before deleting the tree.
    terminations.stop_after = None

    async def stopped(binding: TeamBinding) -> WorktreeDeletionResult:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await teardown_pass(db, runner, datetime.now().astimezone(), stopped)
    assert terminations.ended() == sorted(team.seats)
    assert teardown(world, team) == {}

    # The tree was deleted, then the daemon stopped before the outcome was written.
    assert (await team_tree_deleter(db, None)(team.binding)).success
    await lifecycle_callbacks.reconcile_agent_lifecycle_state(runner)
    assert terminations.ended() == sorted(team.seats)
    assert teardown(world, team) == {"outcome": "cleaned"}
    assert await teardown_pass(db, runner, datetime.now().astimezone()) == 0

    # A refusal's finding was filed, then the daemon stopped before the record named it.
    filed = LocalTaskManager(db).create_task(
        world.case.project_id,
        "Team teardown refused",
        labels=[FINDING_LABEL, f"team-{refused.binding.team_id}"],
        validation_criteria="The refused team tree is deleted or kept with a reason.",
    )
    close(world, refused, commit(refused, "unmerged.py"), minutes_ago=1)
    await lifecycle_callbacks.reconcile_agent_lifecycle_state(runner)
    await lifecycle_callbacks.reconcile_agent_lifecycle_state(runner)

    assert terminations.ended() == sorted([*team.seats, *refused.seats])
    record = teardown(world, refused)
    assert (record["outcome"], record["finding"]) == ("refused", filed.id)
    assert [task.id for task in finding_tasks(world, refused)] == [filed.id]
    assert LocalTaskManager(db).get_task(filed.id).is_escalated
    assert tree_of(refused.binding).is_dir()


async def test_teardown_converges_after_git_removed_the_tree(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", Terminations(db))
    team = await admitted_team(world, "Crashed teardown team")
    task = close(world, team, land(world, team, "crash.py"))
    # Git removed the tree, then the daemon stopped before the tree row was deleted.
    world.case.git("worktree", "remove", "--force", str(tree_of(team.binding)))

    await teardown_pass(db, daemon(db), closed_at(task) + TEARDOWN_GRACE)

    assert teardown(world, team) == {"outcome": "cleaned"}
    assert LocalWorktreeManager(db).get(tree_id_of(team.binding)) is None
    assert not branch_exists(world, team.binding.branch)
    assert finding_tasks(world, team) == []


async def test_teardown_leaves_another_machines_tree_alone(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    team = await admitted_team(world, "Remote teardown team")
    task = close(world, team, land(world, team, "remote.py"))
    elsewhere = str(uuid4())
    LocalMachineManager(db).upsert_seen(elsewhere, TEST_USER_ID)
    db.execute(
        "UPDATE worktrees SET machine_id = %s WHERE id = %s",
        (elsewhere, team.binding.worktree_id),
    )

    # Only the daemon on the tree's machine can prove and delete it.
    assert await teardown_pass(db, daemon(db), closed_at(task) + TEARDOWN_GRACE) == 0

    assert terminations.calls == []
    assert teardown(world, team) == {}
    assert finding_tasks(world, team) == []


async def test_cleanup_preserves_unsafe_trees(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", Terminations(db))
    eligible, dirty, occupied, mismatched, unmerged = [
        await admitted_team(world, f"{name} team tree")
        for name in ("Eligible", "Dirty", "Occupied", "Mismatched", "Unmerged")
    ]
    shas = {
        team.binding.team_id: land(world, team, "landed.py")
        for team in (eligible, dirty, occupied, mismatched)
    }
    shas[unmerged.binding.team_id] = commit(unmerged, "unmerged.py")
    edited = tree_of(dirty.binding) / "README.md"
    edited.write_text("edited after landing\n")
    stranger = world.case.session("operator shell")
    db.execute(
        "UPDATE sessions SET workspace_path = %s WHERE id = %s",
        (occupied.binding.worktree_path, stranger),
    )
    git_in(tree_of(mismatched.binding), "switch", "-c", "operator-experiment")
    closes = [
        close(world, team, shas[team.binding.team_id])
        for team in (eligible, dirty, occupied, mismatched, unmerged)
    ]

    await teardown_pass(db, daemon(db), max(map(closed_at, closes)) + TEARDOWN_GRACE)

    assert teardown(world, eligible) == {"outcome": "cleaned"}
    assert not tree_of(eligible.binding).exists()
    assert not branch_exists(world, eligible.binding.branch)
    refusals = (
        (dirty, "uncommitted_changes"),
        (occupied, "worktree_in_use"),
        (mismatched, "worktree_identity_mismatch"),
        (unmerged, "branch_not_merged_into_target"),
    )
    for team, code in refusals:
        record = teardown(world, team)
        assert (record["outcome"], record["code"]) == ("refused", code)
        (finding,) = finding_tasks(world, team)
        assert record["finding"] == finding.id
        assert finding.is_escalated
        assert FINDING_LABEL in (finding.labels or [])
        assert str(tree_of(team.binding)) in (finding.description or "")
        assert tree_of(team.binding).is_dir()
        assert LocalWorktreeManager(db).get(tree_id_of(team.binding)) is not None
        assert branch_exists(world, team.binding.branch)
    assert edited.read_text() == "edited after landing\n"
    assert git_in(tree_of(mismatched.binding), "branch", "--show-current") == (
        "operator-experiment"
    )
    assert session_status(db, stranger) == "active"


async def test_seat_that_cannot_end_is_a_reported_failure_and_keeps_its_tree(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seat that cannot be ended is a typed, reported teardown failure; its tree stays."""
    db = world.case.db

    async def cannot_end(**_kwargs: Any) -> dict[str, Any]:
        return {"success": False}

    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", cannot_end)
    team = await admitted_team(world, "Seat refuses to end")
    sha = land(world, team, "stuck.py")
    closed = close(world, team, sha)

    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)

    record = teardown(world, team)
    assert record.get("outcome") == "refused"
    assert isinstance(record.get("code"), str) and record["code"]
    (finding,) = finding_tasks(world, team)
    assert record["finding"] == finding.id
    assert finding.is_escalated
    assert FINDING_LABEL in (finding.labels or [])
    assert tree_of(team.binding).is_dir()
    assert LocalWorktreeManager(db).get(tree_id_of(team.binding)) is not None
    assert branch_exists(world, team.binding.branch)


async def test_teardown_deletes_the_tree_a_parked_seat_stopped_occupying(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#25165: a seat parked by a daemon stop, its process gone, must not hold the closed tree."""
    db = world.case.db
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", Terminations(db))
    team = await admitted_team(world, "Parked seat team")
    parked = park(db, team, 1)
    closed = close(world, team, land(world, team, "parked.py"))

    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)

    assert teardown(world, team) == {"outcome": "cleaned"}
    assert finding_tasks(world, team) == []
    assert not tree_of(team.binding).exists()
    assert LocalWorktreeManager(db).get(tree_id_of(team.binding)) is None
    assert not branch_exists(world, team.binding.branch)
    assert {session_status(db, session) for session in team.seats} == {"expired"}
    run = LocalAgentRunManager(db).get(parked.id)
    assert run is not None and run.status == "cancelled"


async def test_delete_ignores_a_parked_seat_whose_run_went_terminal(world: TeamWorld) -> None:
    """#25162: the seats had ended, yet the delete was refused for a parked seat's paused row."""
    db = world.case.db
    team = await admitted_team(world, "Ended seats team")
    park(db, team, 1)
    for session in team.seats:
        if session != team.seats[1]:
            SessionManager(db).update_status(session, "expired")
    close(world, team, land(world, team, "ended.py"))

    result = await team_tree_deleter(db, None)(team.binding)

    assert (result.success, result.error_code, result.error) == (True, None, None)
    assert not tree_of(team.binding).exists()
    assert LocalWorktreeManager(db).get(tree_id_of(team.binding)) is None


async def test_teardown_does_not_escalate_when_every_seat_ended_cooperatively(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#25148: a seat whose run ended stayed paused because its session named an older parked run."""
    db = world.case.db
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", Terminations(db))
    team = await admitted_team(world, "Cooperative team")
    runs = LocalAgentRunManager(db)
    stale = park(db, team, 1)
    relaunched = runs.create(
        parent_session_id=team.runbook.id,
        provider="claude",
        prompt="Resume the test-writer seat.",
        agent_name="test-writer",
        child_session_id=team.seats[1],
    )
    for run in runs.list_by_parent(team.runbook.id):
        # Every seat ends through end_agent_run; only the relaunch's session names another run.
        assert run.child_session_id is not None
        if run.id != relaunched.id:
            SessionManager(db).update_terminal_pickup_metadata(
                run.child_session_id, agent_run_id=run.id
            )
        assert runs.start(run.id) is not None
        assert runs.complete(run.id, result="Seat ended.") is not None
    assert {session_status(db, s) for s in team.seats if s != team.seats[1]} == {"expired"}
    assert session_status(db, team.seats[1]) == "paused"
    seat_row = SessionManager(db).get(team.seats[1])
    assert seat_row is not None and seat_row.agent_run_id == stale.id
    closed = close(world, team, land(world, team, "cooperative.py"))

    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)

    assert teardown(world, team) == {"outcome": "cleaned"}
    assert finding_tasks(world, team) == []
    assert not tree_of(team.binding).exists()
    assert not branch_exists(world, team.binding.branch)
    assert session_status(db, team.seats[1]) == "expired"


async def test_recovery_does_not_relaunch_a_torn_down_seat(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#25165: recovery relaunched a closed team's parked seat into its deleted tree, repeatedly."""
    db = world.case.db
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", Terminations(db))
    resume = AsyncMock(return_value=ResumeAgentResult(success=True))
    monkeypatch.setattr(resume_executor, "resume_agent_run", resume)
    torn_down = await admitted_team(world, "Torn-down team")
    waiting = await admitted_team(world, "Team waiting for its daemon")
    gone = park(db, torn_down, 1)
    parked = park(db, waiting, 1)
    candidates = LocalAgentRunManager(db).list_parked_non_task_resume_candidates(
        machine_id=require_machine_id()
    )
    assert sorted(run.id for run in candidates) == sorted([gone.id, parked.id])
    closed = close(world, torn_down, land(world, torn_down, "gone.py"))
    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)

    resumed = await lifecycle_agents._retry_parked_non_task_resumes(recovering(db))

    assert resumed == 1
    assert [call.args[0].id for call in resume.await_args_list] == [parked.id]
    assert session_status(db, waiting.seats[1]) == "paused", "the open team's seat still waits"


@pytest.mark.asyncio
async def test_team_close_ends_a_dispatched_seat_and_removes_the_tree_without_refusing(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seat the Dev Manager dispatched is not a runbook child, so the run list misses it."""
    db = world.case.db
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    team = await admitted_team(world, "Close over a dispatched seat")
    dispatched = dispatched_seat(world, team, str(tree_of(team.binding)))
    closed = close(world, team, land(world, team, "dispatched.py"))
    later = closed_at(closed) + TEARDOWN_GRACE

    # The first pass may end seats; it must not refuse the tree while a seat is still in it.
    await teardown_pass(db, daemon(db), later)
    assert teardown(world, team).get("outcome") != "refused"
    assert finding_tasks(world, team) == []

    await teardown_pass(db, daemon(db), later)
    assert terminations.ended() == sorted([*team.seats, dispatched])
    assert session_status(db, dispatched) == "expired"
    assert teardown(world, team) == {"outcome": "cleaned"}
    assert not tree_of(team.binding).exists()
    assert finding_tasks(world, team) == []


@pytest.mark.asyncio
async def test_team_close_retries_while_a_dispatched_seat_is_still_exiting(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seat that is signalled but has not exited keeps its tree; a later pass deletes it."""
    db = world.case.db

    async def signal_only(**_kwargs: Any) -> dict[str, Any]:
        return {"success": True}

    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", signal_only)
    team = await admitted_team(world, "Close while a seat exits")
    dispatched = dispatched_seat(world, team, str(tree_of(team.binding)))
    closed = close(world, team, land(world, team, "exiting.py"))
    later = closed_at(closed) + TEARDOWN_GRACE

    await teardown_pass(db, daemon(db), later)
    assert tree_of(team.binding).is_dir()
    assert teardown(world, team).get("outcome") != "refused"
    assert finding_tasks(world, team) == []

    SessionManager(db).update_status(dispatched, "expired")
    await teardown_pass(db, daemon(db), later)
    assert teardown(world, team) == {"outcome": "cleaned"}
    assert not tree_of(team.binding).exists()
    assert finding_tasks(world, team) == []


@pytest.mark.asyncio
async def test_teardown_ends_the_seats_of_a_team_with_no_tree_without_refusing(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    team = await admitted_team(world, "Treeless team")
    sha = land(world, team, "treeless.py")
    # Treeless team mode admits a binding with no worktree, so the record carries none.
    output = world.admit_step_output(team.runbook)
    output["team"]["worktree_id"] = None
    output["team"]["worktree_path"] = None
    world.rewrite_admit_output(team.runbook, output)
    closed = close(world, team, sha)

    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)

    assert terminations.ended() == sorted(team.seats)
    assert {session_status(db, session) for session in team.seats} == {"expired"}
    assert teardown(world, team).get("outcome") != "refused"
    assert finding_tasks(world, team) == []


async def test_treeless_team_closes_its_seats_without_deleting_a_tree(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout_mode none team has no tree: its close ends the seats and never calls delete."""
    db = world.case.db
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    task = world.case.task("Treeless research team")
    runbook = world.child(task)
    binding = await world.admit(runbook, task, checkout_mode="none")
    world.finish(runbook)
    researcher = seat(world, runbook, binding, "researcher")
    team = Team(task, runbook, binding, [researcher])
    closed = close(world, team, commit(team, "report.md"))
    deleted: list[TeamBinding] = []

    async def no_tree_delete(binding: TeamBinding) -> WorktreeDeletionResult:
        deleted.append(binding)
        return WorktreeDeletionResult(success=False, error_code="worktree_not_found")

    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE, delete=no_tree_delete)

    assert deleted == []
    assert terminations.ended() == [researcher]
    assert teardown(world, team) == {"outcome": "cleaned"}


@pytest.mark.parametrize(
    ("runbook_name", "roles"),
    [
        ("team-planning", ("plan-writer", "plan-adversary", "plan-enhancer")),
        ("devteam", ("code-reviewer",)),
    ],
)
@pytest.mark.parametrize("parked", [False, True])
async def test_close_ends_runbook_seats_without_team_admission(
    world: TeamWorld,
    monkeypatch: pytest.MonkeyPatch,
    runbook_name: str,
    roles: tuple[str, ...],
    parked: bool,
) -> None:
    """Saved runbook task ownership also covers planning and separately launched reviewers."""
    db = world.case.db
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    pipeline_dir = Path(__file__).parents[2] / "src/gobby/install/shared/workflows/pipelines"
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((pipeline_dir / f"{runbook_name}.yaml").read_text())
    )
    task = world.case.task("Close runbook without team admission")
    runbook = world.child(task, definition)
    db.execute(
        "UPDATE pipeline_executions SET inputs_json = %s WHERE id = %s",
        (json.dumps({"task": f"#{task.seq_num}"}), execution_id(runbook)),
    )
    seats = [running_seat(world, runbook, role) for role in roles]
    runs = LocalAgentRunManager(db)
    seat_runs = {run.child_session_id: run for run in runs.list_all_by_parent(runbook.id)}
    authored = stage_agent_end_handoff(
        db,
        agent_run_id=seat_runs[seats[0].id].id,
        child_session_id=seats[0].id,
        parent_session_id=runbook.id,
        payload=build_handoff_payload(
            current_state="Plan findings recorded.", next_steps=["Coordinator reviews the plan."]
        ),
    )
    if parked:
        for run in seat_runs.values():
            runs.merge_resume_metadata(run.id, {"provider_native_session_id": f"native-{run.id}"})
            runs.cancel(run.id, terminal_reason="daemon_stop")
    world.finish(runbook)
    open_task = world.case.task("Other task remains open")
    other_runbook = world.child(open_task, definition)
    other = running_seat(world, other_runbook, roles[0])
    reassigned = running_seat(world, runbook, roles[0])
    db.execute(
        "UPDATE agent_runs SET task_id = %s WHERE child_session_id = %s",
        (open_task.id, reassigned.id),
    )
    support_definition = PipelineDefinition.model_validate(
        yaml.safe_load((pipeline_dir / "support.yaml").read_text())
    )
    support_runbook = world.child(task, support_definition)
    db.execute(
        "UPDATE pipeline_executions SET inputs_json = '{}' WHERE id = %s",
        (execution_id(support_runbook),),
    )
    support = running_seat(world, support_runbook, "triage")
    closed = LocalTaskManager(db).close_task(task.id, reason="done")
    runner = daemon(db)

    await teardown_pass(db, runner, closed_at(closed))
    assert terminations.calls == []
    if len(seats) > 1 and not parked:
        terminations.stop_after = 1
        with pytest.raises(asyncio.CancelledError):
            await teardown_pass(db, runner, closed_at(closed) + TEARDOWN_GRACE)
        terminations.stop_after = None
    await teardown_pass(db, runner, closed_at(closed) + TEARDOWN_GRACE)

    expected = [] if parked else sorted(session.id for session in seats)
    assert terminations.ended() == expected
    for session in seats:
        ended_run = runs.get(seat_runs[session.id].id)
        assert ended_run is not None and ended_run.status == "cancelled"
        handoff = get_agent_end_handoff(db, ended_run.id)
        assert handoff is not None
        assert handoff.continuation_session_id == runbook.id
        assert session_status(db, session.id) == "expired"
        if parked:
            assert ended_run.resume_metadata_json is not None
            assert ended_run.resume_metadata_json.get(CONSUMED_BY_KEY) == "teardown"
    assert get_agent_end_handoff(db, seat_runs[seats[0].id].id) == authored
    assert {session_status(db, session.id) for session in (other, reassigned, support)} == {
        "active"
    }
    await teardown_pass(db, runner, closed_at(closed) + TEARDOWN_GRACE)
    assert len(terminations.calls) == len(expected), "a repeated pass never re-ends a seat"


@pytest.mark.parametrize("parked", [False, True])
@pytest.mark.parametrize("inside_tree", [False, True])
async def test_close_ends_task_bound_dispatched_seat_outside_tree_and_preserves_open_work(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch, parked: bool, inside_tree: bool
) -> None:
    db = world.case.db
    team = await admitted_team(world, "Dispatched reviewer in the main checkout")
    reviewer = dispatched_seat(world, team, str(world.case.repo))
    db.execute(
        "UPDATE agent_runs SET task_id = %s WHERE child_session_id = %s",
        (team.task.id, reviewer),
    )
    runs = LocalAgentRunManager(db)
    reviewer_run = runs.get_by_session(reviewer)
    assert reviewer_run is not None
    if parked:
        SessionManager(db).update_terminal_pickup_metadata(reviewer, agent_run_id=reviewer_run.id)
        runs.start(reviewer_run.id)
        runs.merge_resume_metadata(reviewer_run.id, {"provider_native_session_id": "native-review"})
        runs.cancel(reviewer_run.id, terminal_reason="daemon_stop")
    other_task = world.case.task("Another open assignment")
    # Preserve the manager, but still find the children it dispatched for the closing task.
    manager = team.seats[0]
    db.execute(
        "UPDATE agent_runs SET task_id = %s WHERE child_session_id = %s",
        (other_task.id, manager),
    )
    db.execute(
        "UPDATE sessions SET workspace_path = %s WHERE id = %s",
        (team.binding.worktree_path if inside_tree else str(world.case.repo), manager),
    )
    other = dispatched_seat(world, team, str(world.case.repo))
    db.execute(
        "UPDATE agent_runs SET task_id = %s WHERE child_session_id = %s",
        (other_task.id, other),
    )
    reassigned = running_seat(world, team.runbook, "developer")
    LocalTaskManager(db).claim_task(other_task.id, reassigned.id, force=True)
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    closed = close(world, team, land(world, team, "reviewed.py"))

    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)

    assert terminations.ended() == sorted([*team.seats[1:], *([] if parked else [reviewer])])
    assert session_status(db, manager) == "active"
    assert session_status(db, other) == "active"
    assert session_status(db, reassigned.id) == "active"
    assert session_status(db, reviewer) == "expired"
    assert get_agent_end_handoff(db, reviewer_run.id) is not None
    if parked:
        ended_run = runs.get(reviewer_run.id)
        assert ended_run is not None and ended_run.resume_metadata_json is not None
        assert ended_run.resume_metadata_json[CONSUMED_BY_KEY] == "teardown"
    assert LocalTaskManager(db).get_task(other_task.id).claimed_by_session_id == reassigned.id
    assert tree_of(team.binding).exists(), "protected work keeps the tree and teardown due"
    assert teardown(world, team) == {}
    other_closed = LocalTaskManager(db).close_task(other_task.id, reason="done")

    await teardown_pass(db, daemon(db), closed_at(other_closed) + TEARDOWN_GRACE)

    expected = [*team.seats, other, reassigned.id, *([] if parked else [reviewer])]
    assert terminations.ended() == sorted(expected)
    assert {session_status(db, session) for session in (manager, other, reassigned.id)} == {
        "expired"
    }
    assert teardown(world, team) == {"outcome": "cleaned"}
    assert not tree_of(team.binding).exists()


async def test_runbook_seat_refusal_records_one_finding_and_stops_retrying(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    path = (
        Path(__file__).parents[2]
        / "src/gobby/install/shared/workflows/pipelines/team-planning.yaml"
    )
    definition = PipelineDefinition.model_validate(yaml.safe_load(path.read_text()))
    task = world.case.task("Planning seat cannot end")
    runbook = world.child(task, definition)
    planner = running_seat(world, runbook, "plan-writer")
    world.finish(runbook)
    closed = LocalTaskManager(db).close_task(task.id, reason="done")
    calls: list[str] = []

    async def cannot_end(**kwargs: Any) -> dict[str, Any]:
        run: AgentRun = kwargs["run"]
        calls.append(run.id)
        return {"success": False, "error_code": "test_refusal", "error": "seat cannot end"}

    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", cannot_end)
    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)
    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)

    assert len(calls) == 1
    assert session_status(db, planner.id) == "active"
    findings = LocalTaskManager(db).list_tasks(
        project_id=world.case.project_id, label=FINDING_LABEL, closed=False
    )
    assert len(findings) == 1
    assert findings[0].escalated_at is not None
    assert task.id in (findings[0].description or "")
    assert execution_id(runbook) in (findings[0].description or "")
    assert planner.id in (findings[0].description or "")
    outcomes = [
        json.loads(step.output_json or "{}").get("teardown")
        for step in world.executions.get_steps_for_execution(execution_id(runbook))
    ]
    assert {"outcome": "refused", "code": "test_refusal", "finding": findings[0].id} in outcomes
