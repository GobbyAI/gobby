"""An epic leaf's sub-worktree is torn down when the leaf closes (#25224).

The epic's primary seats and tree persist until the epic closes. A leaf whose sub-worktree
is dirty or unmerged keeps that tree and logs the refusal without filing a task.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.team_admission_records import TeamBinding
from gobby.agents.team_teardown import TEARDOWN_GRACE
from gobby.mcp_proxy.tools import agent_cancellation
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.worktrees.deletion import WorktreeDeletionResult
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TeamWorld, git_in, make_team_world
from tests.fixtures.team_teardown_case import (
    TEARDOWN_LOGGER,
    Team,
    Terminations,
    branch_exists,
    closed_at,
    daemon,
    dispatched_seat,
    project_task_ids,
    refusal_codes,
    seated_team,
    session_status,
    teardown_pass,
)

pytestmark = pytest.mark.unit

TITLE = "Scroll and type during daemon outage window"
LEAF_TITLE = "Wire the scroll handler"
NESTED_TITLE = "Scroll state for the nested epic"
# seated_team launches a manager, tester, implementer and reviewer in the epic tree.
PRIMARY_DEVELOPER = 2


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    from gobby.storage.definitions.agents import AgentDefinitionManager
    from gobby.storage.definitions.rules import RuleDefinitionManager

    # Aggregate cleanup is explicit in this synthetic team's policy.
    RuleDefinitionManager(temp_db).create(
        name="fixture-explicit-aggregate-close",
        project_id=sample_project["id"],
        definition_json={
            "event": "task_close",
            "when": "facts.get('closure_intent') == 'ancestor'",
            "effects": [{"type": "block", "reason": "Finish the aggregate explicitly."}],
        },
    )
    AgentDefinitionManager(temp_db).create(
        "default",
        {
            "name": "default",
            "prompts": {"agent": "Exercise team cleanup."},
            "workflows": {"rule_selectors": {"include": ["fixture-explicit-aggregate-close"]}},
        },
        project_id=sample_project["id"],
    )
    return make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )


@dataclass
class SubTree:
    worktree_id: str
    path: Path
    branch: str
    sha: str


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


def sub_tree(world: TeamWorld, team: Team, leaf: Task) -> SubTree:
    """A leaf's sub-worktree cut from epic main, holding one committed change for the leaf."""
    db = world.case.db
    epic_tree = team_tree(team)
    branch = f"epic/{team.task.seq_num}/{leaf.seq_num}"
    path = epic_tree.parent / f"sub-{team.task.seq_num}-{leaf.seq_num}"
    git_in(epic_tree, "worktree", "add", "-b", branch, str(path), team.binding.branch)
    row = LocalWorktreeManager(db).create(
        world.case.project_id,
        branch_name=branch,
        worktree_path=str(path),
        base_branch=team.binding.branch,
        task_id=leaf.id,
    )
    (path / f"leaf_{leaf.seq_num}.py").write_text("work = True\n")
    git_in(path, "add", "--all")
    git_in(path, "commit", "-m", f"[gobby-#{leaf.seq_num}] feat: leaf work")
    return SubTree(row.id, path, branch, git_in(path, "rev-parse", "HEAD"))


def close_leaf(world: TeamWorld, leaf: Task, sha: str) -> Task:
    """A task's committed close, as the Dev Team Lead records it."""
    db = world.case.db
    db.execute("UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([sha]), leaf.id))
    LocalTaskManager(db).close_task(leaf.id, reason="done", closed_commit_sha=sha)
    return LocalTaskManager(db).get_task(leaf.id)


def team_tree(team: Team) -> Path:
    """The epic worktree a team binding names; these tests always create one."""
    assert team.binding.worktree_path is not None
    return Path(team.binding.worktree_path)


@pytest.mark.asyncio
async def test_closing_a_merged_epic_leaf_ends_its_dispatched_seat_and_removes_its_tree(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await seated_team(world, epic, world.child(epic))
    manager, primary = team.seats[0], team.seats[PRIMARY_DEVELOPER]
    tree = team_tree(team)
    sub = sub_tree(world, team, leaf)
    dispatched = dispatched_seat(world, team, str(sub.path))
    git_in(tree, "merge", "--no-ff", sub.branch, "-m", "Merge leaf into epic main")

    # The leaf is still open, so nothing is torn down yet.
    await teardown_pass(db, daemon(db), datetime.now(UTC) + timedelta(minutes=5))
    assert terminations.calls == []
    assert sub.path.is_dir()

    closed = close_leaf(world, leaf, sub.sha)
    tasks_before = project_task_ids(world)
    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)

    assert terminations.ended() == [dispatched]
    assert session_status(db, dispatched) == "expired"
    assert session_status(db, primary) == "active"
    assert session_status(db, manager) == "active"
    assert not sub.path.exists()
    assert LocalWorktreeManager(db).get(sub.worktree_id) is None
    assert not branch_exists(world, sub.branch)
    assert tree.is_dir()
    assert branch_exists(world, team.binding.branch)
    assert LocalTaskManager(db).get_task(epic.id).closed_at is None
    assert project_task_ids(world) == tasks_before


@pytest.mark.asyncio
async def test_leaf_seat_that_cannot_end_files_no_task_and_keeps_its_sub_tree(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#25656: a leaf whose dispatched seat cannot end keeps its sub-tree and files no task."""
    db = world.case.db

    async def cannot_end(**_kwargs: Any) -> dict[str, Any]:
        return {"success": False}

    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", cannot_end)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await seated_team(world, epic, world.child(epic))
    sub = sub_tree(world, team, leaf)
    dispatched_seat(world, team, str(sub.path))
    closed = close_leaf(world, leaf, sub.sha)
    tasks_before = project_task_ids(world)

    with caplog.at_level(logging.WARNING, logger=TEARDOWN_LOGGER):
        await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)

    assert project_task_ids(world) == tasks_before
    assert sub.path.is_dir()
    assert LocalWorktreeManager(db).get(sub.worktree_id) is not None
    assert branch_exists(world, sub.branch)
    (code,) = refusal_codes(caplog)
    assert code


@pytest.mark.asyncio
async def test_leaf_teardown_keeps_a_dirty_or_unmerged_sub_tree_and_logs_it(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = world.case.db
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", Terminations(db))
    epic = new_task(world, TITLE, epic=True)
    dirty_leaf = new_task(world, LEAF_TITLE, parent=epic)
    unmerged_leaf = new_task(world, "Unmerged leaf", parent=epic)
    team = await seated_team(world, epic, world.child(epic))
    tree = team_tree(team)
    dirty = sub_tree(world, team, dirty_leaf)
    unmerged = sub_tree(world, team, unmerged_leaf)
    git_in(tree, "merge", "--no-ff", dirty.branch, "-m", "Merge dirty leaf")
    (dirty.path / "README.md").write_text("edited after the merge\n")
    closes = [
        close_leaf(world, dirty_leaf, dirty.sha),
        close_leaf(world, unmerged_leaf, unmerged.sha),
    ]

    tasks_before = project_task_ids(world)
    with caplog.at_level(logging.WARNING, logger=TEARDOWN_LOGGER):
        await teardown_pass(db, daemon(db), max(map(closed_at, closes)) + TEARDOWN_GRACE)

    assert project_task_ids(world) == tasks_before
    for sub in (dirty, unmerged):
        assert sub.path.is_dir()
        assert LocalWorktreeManager(db).get(sub.worktree_id) is not None
        assert branch_exists(world, sub.branch)
    assert sorted(refusal_codes(caplog)) == [
        "branch_not_merged_into_target",
        "uncommitted_changes",
    ]
    assert (dirty.path / "README.md").read_text() == "edited after the merge\n"
    assert tree.is_dir()


@pytest.mark.asyncio
async def test_refused_leaf_tree_logs_once_per_reason_and_retries_every_pass(
    world: TeamWorld, caplog: pytest.LogCaptureFixture
) -> None:
    """#25656: a retained sub-tree is retried each pass; its refusal logs once per reason."""
    db = world.case.db
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await seated_team(world, epic, world.child(epic))
    tree = team_tree(team)
    sub = sub_tree(world, team, leaf)
    git_in(tree, "merge", "--no-ff", sub.branch, "-m", "Merge leaf into epic main")
    closed = close_leaf(world, leaf, sub.sha)
    refusals = [
        WorktreeDeletionResult(
            success=False, error_code="uncommitted_changes", error="README.md modified"
        ),
        WorktreeDeletionResult(
            success=False, error_code="uncommitted_changes", error="README.md modified"
        ),
        WorktreeDeletionResult(
            success=False, error_code="uncommitted_changes", error="index.lock held"
        ),
        WorktreeDeletionResult(success=False, error_code="worktree_in_use", error="tree in use"),
        WorktreeDeletionResult(success=False, error_code="worktree_in_use", error="tree in use"),
    ]
    calls: list[str | None] = []

    async def refuse(binding: TeamBinding) -> WorktreeDeletionResult:
        calls.append(binding.worktree_id)
        return refusals[len(calls) - 1]

    first_pass = closed_at(closed) + TEARDOWN_GRACE
    with caplog.at_level(logging.DEBUG, logger=TEARDOWN_LOGGER):
        for minutes in range(len(refusals)):
            await teardown_pass(
                db, daemon(db), first_pass + timedelta(minutes=minutes), delete=refuse
            )

    assert len(calls) == len(refusals)
    assert refusal_codes(caplog) == [
        "uncommitted_changes",
        "uncommitted_changes",
        "worktree_in_use",
    ]
    assert refusal_codes(caplog, logging.DEBUG) == ["uncommitted_changes", "worktree_in_use"]
    assert sub.path.is_dir()


def manager_stops(db: HubDatabase, manager: str) -> None:
    """The manager's run cancelled and its session expired, as a daemon stop leaves them."""
    run = db.fetchone(
        "SELECT id::text AS id FROM agent_runs WHERE child_session_id = %s", (manager,)
    )
    assert run is not None
    LocalAgentRunManager(db).cancel(run["id"])
    SessionManager(db).update_status(manager, "expired")


@pytest.mark.asyncio
async def test_leaf_teardown_ends_a_dispatched_seat_whose_manager_already_ended(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await seated_team(world, epic, world.child(epic))
    tree = team_tree(team)
    sub = sub_tree(world, team, leaf)
    dispatched = dispatched_seat(world, team, str(sub.path))
    git_in(tree, "merge", "--no-ff", sub.branch, "-m", "Merge leaf into epic main")
    closed = close_leaf(world, leaf, sub.sha)
    manager_stops(db, team.seats[0])

    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)

    assert terminations.ended() == [dispatched]
    assert session_status(db, dispatched) == "expired"
    assert not sub.path.exists()
    assert LocalWorktreeManager(db).get(sub.worktree_id) is None
    assert not branch_exists(world, sub.branch)
    assert tree.is_dir()


@pytest.mark.asyncio
async def test_closing_the_epic_before_any_pass_ends_every_seat_and_removes_both_trees(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await seated_team(world, epic, world.child(epic))
    tree = team_tree(team)
    sub = sub_tree(world, team, leaf)
    dispatched = dispatched_seat(world, team, str(sub.path))
    git_in(tree, "merge", "--no-ff", sub.branch, "-m", "Merge leaf into epic main")
    closed_leaf = close_leaf(world, leaf, sub.sha)
    closed_epic = close_leaf(world, epic, git_in(tree, "rev-parse", "HEAD"))
    world.case.git("merge", "--no-edit", team.binding.branch)  # the epic lands before it closes
    at = max(closed_at(closed_leaf), closed_at(closed_epic)) + TEARDOWN_GRACE

    await teardown_pass(db, daemon(db), at)

    assert set(terminations.ended()) == {dispatched, *team.seats}
    assert session_status(db, dispatched) == "expired"
    assert not sub.path.exists()
    assert LocalWorktreeManager(db).get(sub.worktree_id) is None
    assert not branch_exists(world, sub.branch)
    # The epic's tree is the sub-tree's merge target, so it stays until the sub-tree is gone.
    assert tree.is_dir()

    await teardown_pass(db, daemon(db), at)

    assert not tree.is_dir()


@pytest.mark.asyncio
async def test_leaf_under_a_nested_epic_ends_its_seat_and_removes_its_sub_tree(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    epic = new_task(world, TITLE, epic=True)
    nested = new_task(world, NESTED_TITLE, parent=epic, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=nested)
    team = await seated_team(world, epic, world.child(epic))
    manager, primary = team.seats[0], team.seats[PRIMARY_DEVELOPER]
    tree = team_tree(team)
    sub = sub_tree(world, team, leaf)
    dispatched = dispatched_seat(world, team, str(sub.path))
    git_in(tree, "merge", "--no-ff", sub.branch, "-m", "Merge leaf into epic main")
    closed = close_leaf(world, leaf, sub.sha)

    await teardown_pass(db, daemon(db), closed_at(closed) + TEARDOWN_GRACE)

    assert terminations.ended() == [dispatched]
    assert session_status(db, dispatched) == "expired"
    assert session_status(db, primary) == "active"
    assert session_status(db, manager) == "active"
    assert not sub.path.exists()
    assert LocalWorktreeManager(db).get(sub.worktree_id) is None
    assert not branch_exists(world, sub.branch)
    assert tree.is_dir()
    assert LocalTaskManager(db).get_task(epic.id).closed_at is None
