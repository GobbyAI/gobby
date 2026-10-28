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
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.worktrees.deletion import WorktreeDeletionResult
from tests.config_runtime_helpers import static_runtime_capture
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.postgres import TEST_USER_ID
from tests.fixtures.team_case import TeamWorld, git_in, make_team_world
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
