"""A closed task's team collapses: its seats end, then its tree is deleted safely (plan 3.3)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import gobby.runner_lifecycle_callbacks as lifecycle_callbacks
from gobby.agents.team_lifecycle import (
    SeatTarget,
    TeamAdmissionRefused,
    TeamBinding,
    check_team_spawn,
)
from gobby.agents.team_teardown import FINDING_LABEL, TEARDOWN_GRACE
from gobby.mcp_proxy.tools import agent_cancellation
from gobby.runner_maintenance.isolation import team_tree_deleter
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.worktrees.deletion import WorktreeDeletionResult
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TeamWorld, git_in, make_team_world
from tests.fixtures.team_teardown_case import (
    Team,
    Terminations,
    admitted_team,
    branch_exists,
    closed_at,
    daemon,
    seat,
    session_status,
    teardown,
    teardown_pass,
)

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
