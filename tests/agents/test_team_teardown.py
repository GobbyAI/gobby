"""A closed task's team collapses: its seats end, then its tree is deleted safely (plan 3.3)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest

import gobby.runner_lifecycle_callbacks as lifecycle_callbacks
from gobby.agents.team_lifecycle import (
    SeatTarget,
    TeamAdmissionRefused,
    TeamBinding,
    check_team_spawn,
)
from gobby.agents.team_teardown import (
    FINDING_LABEL,
    TEARDOWN_GRACE,
    TEARDOWN_KEY,
    TeamTreeDeleter,
    reconcile_team_teardowns,
)
from gobby.mcp_proxy.tools import agent_cancellation
from gobby.runner_maintenance.isolation import team_run_terminator, team_tree_deleter
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.worktrees.deletion import WorktreeDeletionResult
from tests.fixtures.landing_case import bind_task_manager, make_landing_case
from tests.fixtures.team_case import TeamWorld, git_in, make_team_world

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

pytestmark = pytest.mark.unit

ROLES = ("test-writer", "developer", "code-reviewer")


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


@dataclass
class Team:
    task: Task
    runbook: Session
    binding: TeamBinding
    seats: list[str]


@dataclass
class Terminations:
    """What the daemon termination leaves behind: the run cancelled, its session expired.

    ``stop_after`` cancels the pass, as a daemon shutdown does, once that many seats ended.
    """

    db: HubDatabase
    calls: list[dict[str, Any]] = field(default_factory=list)
    stop_after: int | None = None

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        if self.stop_after is not None and len(self.calls) >= self.stop_after:
            raise asyncio.CancelledError
        self.calls.append(kwargs)
        run: AgentRun = kwargs["run"]
        LocalAgentRunManager(self.db).cancel(run.id)
        if run.child_session_id is not None:
            SessionManager(self.db).update_status(run.child_session_id, "expired")
        return {"success": True}

    def ended(self) -> list[str]:
        return sorted(str(call["run"].child_session_id) for call in self.calls)


def seat(
    world: TeamWorld,
    runbook: Session,
    binding: TeamBinding,
    role: str,
    *,
    manages: Task | None = None,
) -> str:
    """A live seat the runbook launched, working in the team tree."""
    db = world.case.db
    session = world.case.session(role, parent=runbook.id)
    db.execute(
        "UPDATE sessions SET workspace_path = %s WHERE id = %s", (binding.worktree_path, session)
    )
    if manages is not None:
        bind_task_manager(
            db, manager=session, parent=runbook.id, project_id=world.case.project_id, task=manages
        )
    else:
        LocalAgentRunManager(db).create(
            parent_session_id=runbook.id,
            provider="claude",
            prompt=f"Run the {role} seat.",
            agent_name=role,
            child_session_id=session,
        )
    return session


async def admitted_team(world: TeamWorld, title: str) -> Team:
    """A devteam whose runbook admitted its tree and launched all four seats."""
    task = world.case.task(title)
    runbook = world.child(task)
    binding = await world.admit(runbook, task)
    world.finish(runbook)
    manager = seat(world, runbook, binding, "dev-manager", manages=task)
    seats = [manager, *(seat(world, runbook, binding, role) for role in ROLES)]
    return Team(task, runbook, binding, seats)


def commit(team: Team, name: str) -> str:
    tree = Path(team.binding.worktree_path)
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


def closed_at(task: Task) -> datetime:
    assert task.closed_at is not None
    return task.closed_at


def teardown(world: TeamWorld, team: Team) -> dict[str, Any]:
    record = world.admit_step_output(team.runbook).get(TEARDOWN_KEY)
    return record if isinstance(record, dict) else {}


def branch_exists(world: TeamWorld, branch: str) -> bool:
    return world.case.git("branch", "--list", branch) != ""


def session_status(db: HubDatabase, session_id: str) -> str:
    session = SessionManager(db).get(session_id)
    assert session is not None
    return session.status


def daemon(db: HubDatabase, *, initialized: bool = True) -> GobbyRunner:
    """The daemon pieces the teardown uses; ``initialized`` False is a start still in progress."""
    init: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    if initialized:
        init.set_result(None)
    return cast(
        "GobbyRunner",
        SimpleNamespace(
            database=db,
            agent_runner=SimpleNamespace(),
            agent_lifecycle_monitor=None,
            completion_registry=None,
            task_manager=LocalTaskManager(db),
            session_manager=SessionManager(db),
            worktree_delete_executor=None,
            _subsystem_init_task=init,
        ),
    )


async def teardown_pass(
    db: HubDatabase, runner: GobbyRunner, at: datetime, delete: TeamTreeDeleter | None = None
) -> int:
    return await reconcile_team_teardowns(
        db,
        terminate=team_run_terminator(runner),
        delete=delete or team_tree_deleter(db, None),
        now=at,
    )


def finding_tasks(world: TeamWorld, team: Team) -> list[Task]:
    return LocalTaskManager(world.case.db).list_tasks(
        project_id=world.case.project_id, label=f"team-{team.binding.team_id}"
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
    assert Path(bystander.binding.worktree_path).is_dir()
    after = LocalTaskManager(db).get_task(team.task.id)
    assert (after.closed_at, after.commits, after.closed_commit_sha) == (
        closed.closed_at,
        [sha],
        sha,
    )
    assert world.case.git("merge-base", "--is-ancestor", sha, "trunk") == ""
    assert teardown(world, team) == {"outcome": "cleaned"}
    assert not Path(team.binding.worktree_path).exists()
    assert LocalWorktreeManager(db).get(team.binding.worktree_id) is None
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
    )
    close(world, refused, commit(refused, "unmerged.py"), minutes_ago=1)
    await lifecycle_callbacks.reconcile_agent_lifecycle_state(runner)
    await lifecycle_callbacks.reconcile_agent_lifecycle_state(runner)

    assert terminations.ended() == sorted([*team.seats, *refused.seats])
    record = teardown(world, refused)
    assert (record["outcome"], record["finding"]) == ("refused", filed.id)
    assert [task.id for task in finding_tasks(world, refused)] == [filed.id]
    assert LocalTaskManager(db).get_task(filed.id).is_escalated
    assert Path(refused.binding.worktree_path).is_dir()


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
    edited = Path(dirty.binding.worktree_path) / "README.md"
    edited.write_text("edited after landing\n")
    stranger = world.case.session("operator shell")
    db.execute(
        "UPDATE sessions SET workspace_path = %s WHERE id = %s",
        (occupied.binding.worktree_path, stranger),
    )
    git_in(mismatched.binding.worktree_path, "switch", "-c", "operator-experiment")
    closes = [
        close(world, team, shas[team.binding.team_id])
        for team in (eligible, dirty, occupied, mismatched, unmerged)
    ]

    await teardown_pass(db, daemon(db), max(map(closed_at, closes)) + TEARDOWN_GRACE)

    assert teardown(world, eligible) == {"outcome": "cleaned"}
    assert not Path(eligible.binding.worktree_path).exists()
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
        assert team.binding.worktree_path in (finding.description or "")
        assert Path(team.binding.worktree_path).is_dir()
        assert LocalWorktreeManager(db).get(team.binding.worktree_id) is not None
        assert branch_exists(world, team.binding.branch)
    assert edited.read_text() == "edited after landing\n"
    assert git_in(mismatched.binding.worktree_path, "branch", "--show-current") == (
        "operator-experiment"
    )
    assert session_status(db, stranger) == "active"
