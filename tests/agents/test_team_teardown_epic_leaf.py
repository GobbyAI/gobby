"""An epic leaf's sub-worktree is torn down when the leaf closes (#25224).

The epic's primary seats and tree persist until the epic closes. A leaf whose sub-worktree
is dirty or unmerged keeps that tree and reports it to Triage as a finding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.team_teardown import FINDING_LABEL, TEARDOWN_GRACE
from gobby.mcp_proxy.tools import agent_cancellation
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TeamWorld, git_in, make_team_world
from tests.fixtures.team_teardown_case import (
    Team,
    Terminations,
    branch_exists,
    closed_at,
    daemon,
    dispatched_seat,
    seated_team,
    session_status,
    teardown_pass,
)

pytestmark = pytest.mark.unit

TITLE = "Scroll and type during daemon outage window"
LEAF_TITLE = "Wire the scroll handler"
# seated_team launches [dev-manager, test-writer, developer, code-reviewer] in the epic tree.
PRIMARY_DEVELOPER = 2


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
    epic_tree = Path(team.binding.worktree_path)
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
    """The leaf's committed close, as the Dev Manager records it."""
    db = world.case.db
    db.execute("UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([sha]), leaf.id))
    LocalTaskManager(db).close_task(leaf.id, reason="done", closed_commit_sha=sha)
    return LocalTaskManager(db).get_task(leaf.id)


def leaf_tickets(world: TeamWorld, tree: Path) -> list[Task]:
    """Triage findings that name a sub-worktree path."""
    return [
        task
        for task in LocalTaskManager(world.case.db).list_tasks(
            project_id=world.case.project_id, label=FINDING_LABEL
        )
        if str(tree) in (task.description or "")
    ]


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
    tree = Path(team.binding.worktree_path)
    sub = sub_tree(world, team, leaf)
    dispatched = dispatched_seat(world, team, str(sub.path))
    git_in(tree, "merge", "--no-ff", sub.branch, "-m", "Merge leaf into epic main")

    # The leaf is still open, so nothing is torn down yet.
    await teardown_pass(db, daemon(db), datetime.now(UTC) + timedelta(minutes=5))
    assert terminations.calls == []
    assert sub.path.is_dir()

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
    assert branch_exists(world, team.binding.branch)
    assert LocalTaskManager(db).get_task(epic.id).closed_at is None
    assert leaf_tickets(world, sub.path) == []


@pytest.mark.asyncio
async def test_leaf_teardown_keeps_a_dirty_or_unmerged_sub_tree_and_reports_it(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world.case.db
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", Terminations(db))
    epic = new_task(world, TITLE, epic=True)
    dirty_leaf = new_task(world, LEAF_TITLE, parent=epic)
    unmerged_leaf = new_task(world, "Unmerged leaf", parent=epic)
    team = await seated_team(world, epic, world.child(epic))
    tree = Path(team.binding.worktree_path)
    dirty = sub_tree(world, team, dirty_leaf)
    unmerged = sub_tree(world, team, unmerged_leaf)
    git_in(tree, "merge", "--no-ff", dirty.branch, "-m", "Merge dirty leaf")
    (dirty.path / "README.md").write_text("edited after the merge\n")
    closes = [
        close_leaf(world, dirty_leaf, dirty.sha),
        close_leaf(world, unmerged_leaf, unmerged.sha),
    ]

    await teardown_pass(db, daemon(db), max(map(closed_at, closes)) + TEARDOWN_GRACE)

    for sub in (dirty, unmerged):
        assert sub.path.is_dir()
        assert LocalWorktreeManager(db).get(sub.worktree_id) is not None
        assert branch_exists(world, sub.branch)
        (ticket,) = leaf_tickets(world, sub.path)
        assert ticket.is_escalated
    assert (dirty.path / "README.md").read_text() == "edited after the merge\n"
    assert tree.is_dir()
