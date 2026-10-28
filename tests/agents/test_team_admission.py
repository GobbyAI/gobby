"""Single-use team admission: one task, one task-titled tree, bound to the admitting execution."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.agents.team_lifecycle import TeamAdmissionRefused, TeamBinding
from gobby.code_index.gcode_gateway import GcodeGateway, GcodeGatewayError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import (
    SLUG,
    TITLE,
    TeamWorld,
    execution_id,
    git_in,
    make_team_world,
)

pytestmark = pytest.mark.unit


def tree_of(binding: TeamBinding) -> Path:
    """The team tree a binding names; these tests always admit a team with one."""
    assert binding.worktree_path is not None
    return Path(binding.worktree_path)


def tree_id_of(binding: TeamBinding) -> str:
    """The team tree id a binding names; these tests always admit a team with one."""
    assert binding.worktree_id is not None
    return binding.worktree_id


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


async def test_idempotent_team_admission(world: TeamWorld) -> None:
    task = world.case.task(TITLE)
    first, second = world.child(task), world.child(task)

    results = await asyncio.gather(
        world.admit(first, task), world.admit(second, task), return_exceptions=True
    )

    teams = [result for result in results if isinstance(result, TeamBinding)]
    refusals = [result for result in results if isinstance(result, TeamAdmissionRefused)]
    assert (len(teams), len(refusals)) == (1, 1), results
    team = teams[0]
    assert refusals[0].code == "team_exists"
    winner, loser = (first, second) if team.team_id == execution_id(first) else (second, first)
    assert team.team_id == execution_id(winner)

    branch = f"task-{task.seq_num}-{SLUG}"
    assert (team.task_id, team.project_id, team.branch, team.base_branch) == (
        task.id,
        world.case.project_id,
        branch,
        "trunk",
    )
    assert tree_of(team).name == branch
    assert git_in(tree_of(team), "branch", "--show-current") == branch
    assert world.task_branches() == [branch]
    assert world.tree_ids() == [team.worktree_id]

    # A duplicate launch from the admitted execution returns the same team and tree.
    assert await world.admit(winner, task) == team
    with session_context_for_test(winner.id):
        reply = await world.tools.call("admit_team", {"task": f"#{task.seq_num}"})
    assert reply == {
        "success": True,
        "team": asdict(team),
        "task_id": task.id,
        "branch": branch,
        "worktree": team.worktree_id,
        "project_path": team.worktree_path,
    }
    # The refused execution stays refused, and nothing new was created.
    with pytest.raises(TeamAdmissionRefused) as again:
        await world.admit(loser, task)
    assert again.value.code == "team_exists"
    assert world.admit_step_output(loser) == {}
    assert world.task_branches() == [branch]
    assert world.tree_ids() == [team.worktree_id]


async def test_replacement_stays_in_admitted_team(world: TeamWorld) -> None:
    task = world.case.task(TITLE)
    original = world.child(task)
    team = await world.admit(original, task)
    tree = tree_of(team)
    (tree / "work.txt").write_text("committed by the developer\n")
    git_in(tree, "add", "work.txt")
    git_in(tree, "commit", "-m", "developer work")
    head = git_in(tree, "rev-parse", "HEAD")
    (tree / "draft.txt").write_text("uncommitted draft\n")

    # Partial launch: the admission recorded its team and made the tree, then the step died
    # before the binding was complete. The re-run admit step adopts the same tree.
    intent = {
        key: value
        for key, value in asdict(team).items()
        if key not in ("worktree_id", "worktree_path")
    }
    world.rewrite_admit_output(original, {"team": intent})
    assert await world.admit(original, task) == team

    # The admitting execution finishes; a replacement execution rejoins the same team.
    world.finish(original)
    replacement = world.child(task)
    assert await world.admit(replacement, task, team_id=team.team_id) == team
    assert world.admit_step_output(replacement)["team"] == asdict(team)

    reply = await world.spawn(replacement, worktree_id=team.worktree_id, checkout_mode="none")
    assert reply["success"] is True, reply
    assert [launch["worktree_id"] for launch in world.launches] == [team.worktree_id]

    # Existing work in the team tree is untouched by recovery and replacement.
    assert git_in(tree, "rev-parse", "HEAD") == head
    assert (tree / "draft.txt").read_text() == "uncommitted draft\n"
    assert world.tree_ids() == [team.worktree_id]
    assert world.task_branches() == [team.branch]


async def test_team_binding_cannot_drift(world: TeamWorld, tmp_path: Path) -> None:
    task, other = world.case.task(TITLE), world.case.task("Another task")
    owner = world.child(task)
    team = await world.admit(owner, task)
    elsewhere = LocalWorktreeManager(world.case.db).create(
        world.case.project_id,
        branch_name="elsewhere",
        worktree_path=str(tmp_path / "elsewhere"),
        base_branch="trunk",
        task_id=other.id,
    )

    # Admission cannot rebind the team to another task or team.
    stranger, duplicate = world.child(other), world.child(task)
    attempts: list[tuple[Callable[[], Awaitable[TeamBinding]], str]] = [
        (lambda: world.admit(owner, other), "team_binding_drift"),
        (lambda: world.admit(owner, task, team_id=execution_id(stranger)), "team_binding_drift"),
        (lambda: world.admit(stranger, other, team_id=team.team_id), "team_task_mismatch"),
        (lambda: world.admit(duplicate, task, team_id="not-a-team"), "team_missing"),
        (lambda: world.admit(duplicate, task), "team_exists"),
    ]
    for attempt, code in attempts:
        with pytest.raises(TeamAdmissionRefused) as refused:
            await attempt()
        assert refused.value.code == code

    # Seat spawns cannot leave the admitted tree or carry another task.
    spawns: list[tuple[dict[str, Any], str]] = [
        ({"checkout_mode": "worktree"}, "team_tree_fixed"),
        ({"worktree_id": team.worktree_id, "branch_name": "task-new"}, "team_tree_fixed"),
        ({"worktree_id": team.worktree_id, "base_branch": "trunk"}, "team_tree_fixed"),
        ({"clone_id": "clone"}, "team_tree_fixed"),
        ({"worktree_id": elsewhere.id}, "team_tree_mismatch"),
        ({"project_path": str(world.case.repo)}, "team_tree_mismatch"),
        ({}, "team_tree_required"),
        ({"worktree_id": team.worktree_id, "task_id": other.id}, "team_task_mismatch"),
        ({"reserved_run_id": None, "checkout_mode": "worktree"}, "team_tree_fixed"),
    ]
    for target, code in spawns:
        reply = await world.spawn(owner, **target)
        assert (reply["success"], reply.get("error_code")) == (False, code), target
    assert world.launches == []

    allowed = await world.spawn(owner, worktree_id=team.worktree_id, task_id=f"#{task.seq_num}")
    assert allowed["success"] is True, allowed
    joined = await world.spawn(owner, project_path=team.worktree_path)
    assert joined["success"] is True, joined
    assert world.task_branches() == [team.branch]
    assert sorted(world.tree_ids()) == sorted([tree_id_of(team), elsewhere.id])


async def test_team_tree_is_pinned_for_code_index(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seats join the team tree without an isolation preflight, so admission must pin
    its overlay; an unpinned overlay hides every file unchanged from the parent index."""
    pin = AsyncMock()
    monkeypatch.setattr(GcodeGateway, "pin_project", pin)
    project_json = Path(world.case.repo) / ".gobby" / "project.json"
    project_json.parent.mkdir(exist_ok=True)
    project_json.write_text(json.dumps({"id": world.case.project_id}))
    task = world.case.task(TITLE)

    team = await world.admit(world.child(task), task)

    # `gcode pin` copies the ancestor's selectors at the marker's base commit.
    marker = json.loads((tree_of(team) / ".gobby" / "isolation.json").read_text())
    assert Path(marker["ancestor_checkout_path"]).resolve() == Path(world.case.repo).resolve()
    assert marker["base_commit"] == git_in(tree_of(team), "rev-parse", "HEAD")
    pin.assert_awaited_once_with(tree_of(team))


async def test_team_tree_pin_failure_still_admits(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The code index is advisory: a failed pin never refuses the team or loses its tree."""
    monkeypatch.setattr(
        GcodeGateway, "pin_project", AsyncMock(side_effect=GcodeGatewayError("index down"))
    )
    task = world.case.task(TITLE)

    team = await world.admit(world.child(task), task)

    assert git_in(tree_of(team), "branch", "--show-current") == team.branch
    assert world.tree_ids() == [team.worktree_id]


async def test_short_team_id_resolves_or_names_the_full_uuid(world: TeamWorld) -> None:
    """A short team_id prefix joins its team, or refuses by naming the full-UUID requirement."""
    task = world.case.task(TITLE)
    original = world.child(task)
    team = await world.admit(original, task)
    world.finish(original)
    replacement = world.child(task)

    outcome: TeamBinding | TeamAdmissionRefused
    try:
        outcome = await world.admit(replacement, task, team_id=team.team_id[:8])
    except TeamAdmissionRefused as refused:
        outcome = refused

    assert outcome == team or re.search(r"full.*uuid|uuid.*full", str(outcome), re.I), outcome


async def _land_team_commit(world: TeamWorld, task: Task, team: TeamBinding, name: str) -> str:
    """Commit ``name`` in the team tree and land that commit on ``task``."""
    tree = tree_of(team)
    (tree / name).write_text(f"{name}\n")
    git_in(tree, "add", name)
    git_in(tree, "commit", "-m", f"Add {name}")
    sha = git_in(tree, "rev-parse", "HEAD")
    world.case.link(task, sha)
    world.case.approve(task, sha)
    landed = await world.case.land(task, sha)
    assert landed["landed"] is True, landed
    return sha


async def test_landed_open_task_keeps_its_team_tree_admissible(world: TeamWorld) -> None:
    """A landing on a task that stays open leaves the task's admitted tree admissible."""
    task = world.case.task(TITLE)
    original = world.child(task)
    team = await world.admit(original, task)
    await _land_team_commit(world, task, team, "landed.txt")
    assert LocalTaskManager(world.case.db).get_task(task.id).closed_at is None

    # The team keeps working after the landing, so its HEAD holds a commit the landing lacks.
    tree = tree_of(team)
    (tree / "follow_up.txt").write_text("follow-up after the landing\n")
    git_in(tree, "add", "follow_up.txt")
    git_in(tree, "commit", "-m", "Follow-up after the landing")

    assert await world.admit(original, task) == team
    rejoined = await world.admit(world.child(task), task, team_id=team.team_id)
    assert rejoined == team
    assert world.tree_ids() == [team.worktree_id]
    assert world.task_branches() == [team.branch]

    # A fresh execution without team_id is refused as a second team, not given a new tree.
    with pytest.raises(TeamAdmissionRefused) as again:
        await world.admit(world.child(task), task)
    assert again.value.code == "team_exists"
    assert world.tree_ids() == [team.worktree_id]


async def test_closed_task_refuses_its_landed_team(world: TeamWorld) -> None:
    """Once its landed task closes, the task's team is refused rather than re-admitted."""
    task = world.case.task(TITLE)
    original = world.child(task)
    team = await world.admit(original, task)
    sha = await _land_team_commit(world, task, team, "landed.txt")
    LocalTaskManager(world.case.db).close_task(
        task.id, reason="done", closed_in_session_id=original.id, closed_commit_sha=sha
    )

    with pytest.raises(TeamAdmissionRefused) as refused:
        await world.admit(world.child(task), task, team_id=team.team_id)
    assert refused.value.code == "team_task_closed"


async def test_treeless_team_admits_without_a_tree(world: TeamWorld) -> None:
    """A checkout_mode none team has no tree; it names the main checkout's branch and joins."""
    task = world.case.task(TITLE)
    first = world.child(task)
    team = await world.admit(first, task, checkout_mode="none")

    assert (team.task_id, team.project_id, team.branch, team.base_branch) == (
        task.id,
        world.case.project_id,
        "trunk",
        "trunk",
    )
    assert team.worktree_id is None
    assert team.worktree_path is not None
    assert Path(team.worktree_path).is_dir()
    assert world.task_branches() == []
    assert world.tree_ids() == []

    # A duplicate launch returns the same team; a second execution for the task joins it.
    assert await world.admit(first, task, checkout_mode="none") == team
    second = world.child(task)
    assert await world.admit(second, task, checkout_mode="none") == team
    with session_context_for_test(second.id):
        reply = await world.tools.call(
            "admit_team", {"task": f"#{task.seq_num}", "checkout_mode": "none"}
        )
    assert (reply["success"], reply["team"]) == (True, asdict(team))
    assert world.task_branches() == []
    assert world.tree_ids() == []
