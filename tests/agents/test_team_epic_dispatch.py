"""Declared coordinator authority and safe task-team worktree adoption."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.agents.team_admission_records import TeamAdmissionRefused, TeamBinding
from gobby.agents.team_lifecycle import managed_task_team
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.tasks.land_commit import land_candidate, task_landing_target
from gobby.workflows.state_manager import SessionVariableManager
from gobby.worktrees.deletion import (
    DeletionSurface,
    WorktreeDeletionRequest,
    WorktreeDeletionResult,
    delete_worktree_transaction,
)
from gobby.worktrees.executor import run_worktree_delete
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import (
    TITLE,
    TeamWorld,
    git_in,
    make_team_world,
    running_seat,
    synthetic_manager_body,
)

pytestmark = pytest.mark.unit
_REAL_AGENT_BODY = _factory._load_agent_body
LEAF_TITLE = "Wire the scroll handler"
DEFINITIONS: dict[str, tuple[str, ...]] = {"developer": ("implementation",)}


@dataclass
class EpicTeam:
    world: TeamWorld
    epic: Task
    runbook: Session
    binding: TeamBinding
    manager: str

    async def spawn(self, agent: str, **target: Any) -> dict[str, Any]:
        return await self.world.direct_spawn(self.manager, agent=agent, **target)


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    world = make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )
    monkeypatch.setattr(_factory, "_load_agent_body", _REAL_AGENT_BODY)
    runs = LocalAgentRunManager(temp_db)

    async def launch(**kwargs: Any) -> dict[str, Any]:
        """Record the launch and start a run for it, as the real launcher would."""
        world.launches.append(kwargs)
        run = runs.create(
            parent_session_id=kwargs["parent_session_id"] or world.case.creator,
            provider="claude",
            prompt=kwargs["prompt"],
            agent_name=kwargs["agent_lookup_name"],
            task_id=kwargs.get("task_id"),
            worktree_id=kwargs.get("worktree_id"),
        )
        runs.start(run.id)
        return {"success": True, "run_id": run.id}

    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)
    project_id = world.case.project_id
    definitions = AgentDefinitionManager(temp_db)
    for name, steps in DEFINITIONS.items():
        definitions.create(name, _body(name, steps), project_id=project_id)
    # A synthetic coordinator carries the team_manager grant and spawn scope under test.
    manager_body = synthetic_manager_body()
    manager_body["name"] = "fixture-coordinator"
    manager_body["spawnable_agents"] = list(DEFINITIONS)
    definitions.create("fixture-coordinator", manager_body, project_id=project_id)
    return world


def tree_of(binding: TeamBinding) -> Path:
    """The team tree a binding names; these tests always admit a team with one."""
    assert binding.worktree_path is not None
    return Path(binding.worktree_path)


def tree_id_of(binding: TeamBinding) -> str:
    """The team tree id a binding names; these tests always admit a team with one."""
    assert binding.worktree_id is not None
    return binding.worktree_id


def _body(name: str, steps: tuple[str, ...]) -> dict[str, Any]:
    return {
        "name": name,
        "version": "1.0",
        "prompts": {"agent": f"Run the {name} seat."},
        "workflows": {"rule_selectors": {"include": []}},
        "task_attestation_writes": list(steps),
    }


def new_task(
    world: TeamWorld,
    title: str,
    *,
    parent: Task | None = None,
    epic: bool = False,
    domain: str | None = None,
) -> Task:
    return LocalTaskManager(world.case.db).create_task(
        world.case.project_id,
        title,
        parent_task_id=None if parent is None else parent.id,
        created_in_session_id=world.case.creator,
        task_type="epic" if epic else "task",
        validation_criteria=None if epic else "The leaf works.",
        implementation_domain=domain,
    )


async def epic_team(
    world: TeamWorld,
    epic: Task,
    tab_title: str | None = None,
    *,
    manager_name: str = "fixture-coordinator",
) -> EpicTeam:
    """An admitted epic team with its running coordinator seat, which never claims."""
    db = world.case.db
    runbook = world.child(epic)
    binding = await world.admit(runbook, epic, tab_title=tab_title or f"Dev Team - #{epic.seq_num}")
    manager = world.case.session(manager_name, parent=runbook.id)
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent_session_id=runbook.id,
        provider="claude",
        prompt="Manage the epic team.",
        agent_name=manager_name,
        child_session_id=manager,
        task_id=epic.id,
    )
    runs.start(run.id)
    SessionVariableManager(db).merge_variables(manager, {"_agent_type": manager_name})
    SessionManager(db).update_terminal_pickup_metadata(manager, agent_run_id=run.id)
    return EpicTeam(world, epic, runbook, binding, manager)


@pytest.mark.asyncio
@pytest.mark.parametrize("manager_name", ["researcher", "fixture-manager"])
@pytest.mark.parametrize("granted", [True, False])
async def test_managed_task_team_uses_declared_manager_policy(
    world: TeamWorld, manager_name: str, granted: bool
) -> None:
    body = synthetic_manager_body()
    body["name"] = manager_name
    body["workflows"]["variables"]["team_manager"] = granted
    AgentDefinitionManager(world.case.db).create(
        manager_name, body, project_id=world.case.project_id
    )
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await epic_team(world, epic, manager_name=manager_name)

    managed = managed_task_team(world.case.db, team.manager, leaf.id)

    if granted:
        assert managed is not None
        assert managed.team_id == team.binding.team_id
    else:
        assert managed is None


async def delete_tree(
    world: TeamWorld, worktree_id: str, merged_into: str
) -> WorktreeDeletionResult:
    """The existing deletion path a coordinator uses for a finished sub-worktree."""
    db = world.case.db

    def git(_tree: Any) -> WorktreeGitManager:
        return WorktreeGitManager(str(world.case.repo))

    return await run_worktree_delete(
        None,
        partial(
            delete_worktree_transaction,
            request=WorktreeDeletionRequest(
                worktree_id, DeletionSurface.MCP, merged_into=merged_into
            ),
            worktree_storage=LocalWorktreeManager(db),
            resolve_git_manager=git,
            task_manager=LocalTaskManager(db),
        ),
    )


@pytest.mark.asyncio
async def test_merged_leaf_tree_cleanup(world: TeamWorld) -> None:
    db = world.case.db
    epic = new_task(world, TITLE, epic=True)
    merged_leaf = new_task(world, LEAF_TITLE, parent=epic)
    open_leaf = new_task(world, "Unmerged leaf", parent=epic)
    team = await epic_team(world, epic)
    epic_tree = tree_of(team.binding)

    async def sub_tree(leaf: Task) -> tuple[str, Path, str]:
        """Register a clean tree to exercise the low-level merge/deletion guard."""
        branch = f"epic/{epic.seq_num}/{leaf.seq_num}"
        path = epic_tree.parent / f"sub-{epic.seq_num}-{leaf.seq_num}"
        git_in(epic_tree, "worktree", "add", "-b", branch, str(path), team.binding.branch)
        row = LocalWorktreeManager(db).create(
            world.case.project_id,
            branch_name=branch,
            worktree_path=str(path),
            base_branch=team.binding.branch,
            task_id=leaf.id,
        )
        (path / f"leaf_{leaf.seq_num}.py").write_text("work = True\n")
        git_in(path, "add", f"leaf_{leaf.seq_num}.py")
        git_in(path, "commit", "-m", f"[gobby-#{leaf.seq_num}] feat: leaf work")
        return row.id, path, branch

    merged_id, merged_path, merged_branch = await sub_tree(merged_leaf)
    open_id, open_path, open_branch = await sub_tree(open_leaf)

    git_in(epic_tree, "merge", "--no-ff", merged_branch, "-m", "Merge leaf into epic main")
    assert len(git_in(epic_tree, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3

    done = await delete_tree(world, merged_id, team.binding.branch)
    assert done.success, done
    assert not merged_path.exists()
    assert world.case.git("branch", "--list", merged_branch) == ""

    refused = await delete_tree(world, open_id, team.binding.branch)
    assert not refused.success, refused
    assert open_path.exists()
    assert world.case.git("branch", "--list", open_branch) != ""


def leaf_tree(
    team: EpicTeam,
    epic: Task,
    leaf: Task,
    *,
    branch: str | None = None,
    base: str | None = None,
    dirty: bool = False,
) -> tuple[str, Path, str]:
    """A tree a finished pod left on its leaf: one commit of leaf work, or dirty with none."""
    epic_tree = tree_of(team.binding)
    path = epic_tree.parent / f"leaf-{epic.seq_num}-{leaf.seq_num}"
    branch = branch or f"epic/{epic.seq_num}/{leaf.seq_num}"
    git_in(epic_tree, "worktree", "add", "-b", branch, str(path), team.binding.branch)
    row = LocalWorktreeManager(team.world.case.db).create(
        team.world.case.project_id,
        branch_name=branch,
        worktree_path=str(path),
        base_branch=base or team.binding.branch,
        task_id=leaf.id,
    )
    if dirty:
        (path / "scratch.txt").write_text("unsaved\n")
    else:
        (path / f"leaf_{leaf.seq_num}.py").write_text("work = True\n")
        git_in(path, "add", f"leaf_{leaf.seq_num}.py")
        git_in(path, "commit", "-m", f"[gobby-#{leaf.seq_num}] feat: leaf work")
    return row.id, path, branch


@pytest.mark.asyncio
async def test_pod_adopts_its_leafs_clean_tree_on_the_leaf_branch(world: TeamWorld) -> None:
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await epic_team(world, epic)
    tree_id, path, branch = leaf_tree(team, epic, leaf)
    head = git_in(path, "rev-parse", "HEAD")
    trees = sorted(world.tree_ids())

    pod = await world.admit(
        world.child(leaf, parent=team.manager), leaf, parent_team_id=team.binding.team_id
    )

    assert pod.worktree_id == tree_id
    assert tree_of(pod) == path
    assert pod.branch == branch
    assert pod.base_branch == team.binding.branch
    assert git_in(path, "branch", "--show-current") == branch
    assert git_in(path, "rev-parse", "HEAD") == head
    assert sorted(world.tree_ids()) == trees


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tree_kwargs",
    [{"branch": "task-stray"}, {"base": "main"}, {"dirty": True}],
    ids=["other_branch", "other_base", "dirty"],
)
async def test_pod_refuses_a_leaf_tree_it_cannot_adopt(
    world: TeamWorld, tree_kwargs: dict[str, Any]
) -> None:
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await epic_team(world, epic)
    _, path, _ = leaf_tree(team, epic, leaf, **tree_kwargs)
    trees = sorted(world.tree_ids())

    with pytest.raises(TeamAdmissionRefused) as refused:
        await world.admit(
            world.child(leaf, parent=team.manager), leaf, parent_team_id=team.binding.team_id
        )

    assert refused.value.code == "team_exists"
    assert sorted(world.tree_ids()) == trees
    assert path.is_dir()


@pytest.mark.asyncio
async def test_pod_adopts_its_leaf_tree_once_the_prior_pod_has_ended(world: TeamWorld) -> None:
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await epic_team(world, epic)
    prior = world.child(leaf, parent=team.manager)
    first = await world.admit(prior, leaf, parent_team_id=team.binding.team_id)
    world.finish(prior)
    trees = sorted(world.tree_ids())

    pod = await world.admit(
        world.child(leaf, parent=team.manager), leaf, parent_team_id=team.binding.team_id
    )

    assert pod.worktree_id == first.worktree_id
    assert pod.branch == first.branch
    assert tree_of(pod) == tree_of(first)
    assert sorted(world.tree_ids()) == trees


@pytest.mark.asyncio
async def test_pod_refuses_a_leaf_tree_a_prior_seat_still_works_in(world: TeamWorld) -> None:
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await epic_team(world, epic)
    prior = world.child(leaf, parent=team.manager)
    await world.admit(prior, leaf, parent_team_id=team.binding.team_id)
    running_seat(world, prior, "developer")
    world.finish(prior)
    trees = sorted(world.tree_ids())

    with pytest.raises(TeamAdmissionRefused) as refused:
        await world.admit(
            world.child(leaf, parent=team.manager), leaf, parent_team_id=team.binding.team_id
        )

    assert refused.value.code == "team_exists"
    assert sorted(world.tree_ids()) == trees


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True], ids=["pending", "running"])
async def test_pod_refuses_a_leaf_tree_an_unattached_seat_still_holds(
    world: TeamWorld, started: bool
) -> None:
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await epic_team(world, epic)
    prior = world.child(leaf, parent=team.manager)
    await world.admit(prior, leaf, parent_team_id=team.binding.team_id)
    runs = LocalAgentRunManager(world.case.db)
    seat = runs.create(
        parent_session_id=prior.id,
        provider="claude",
        prompt="Implement the leaf.",
        agent_name="developer",
    )
    if started:
        assert runs.start(seat.id) is not None
    world.finish(prior)
    trees = sorted(world.tree_ids())

    with pytest.raises(TeamAdmissionRefused) as refused:
        await world.admit(
            world.child(leaf, parent=team.manager), leaf, parent_team_id=team.binding.team_id
        )

    assert refused.value.code == "team_exists"
    assert sorted(world.tree_ids()) == trees


@pytest.mark.asyncio
async def test_pod_adopts_its_leaf_tree_once_an_unattached_seat_has_ended(world: TeamWorld) -> None:
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await epic_team(world, epic)
    prior = world.child(leaf, parent=team.manager)
    first = await world.admit(prior, leaf, parent_team_id=team.binding.team_id)
    runs = LocalAgentRunManager(world.case.db)
    seat = runs.create(
        parent_session_id=prior.id,
        provider="claude",
        prompt="Implement the leaf.",
        agent_name="developer",
    )
    runs.complete(seat.id)
    world.finish(prior)

    pod = await world.admit(
        world.child(leaf, parent=team.manager), leaf, parent_team_id=team.binding.team_id
    )

    assert pod.worktree_id == first.worktree_id
    assert tree_of(pod) == tree_of(first)


@pytest.mark.asyncio
@pytest.mark.parametrize("diverged_parent", [False, True], ids=["fast-forward", "merge"])
@pytest.mark.parametrize("dirty_parent", [False, True], ids=["clean", "dirty"])
async def test_leaf_lands_into_parent_team_branch(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch, diverged_parent: bool, dirty_parent: bool
) -> None:
    monkeypatch.setattr("gobby.tasks.land_commit._refresh_index", AsyncMock())
    epic = new_task(world, "Parent integration", epic=True)
    leaf = new_task(world, "Child delivery", parent=epic)
    team = await epic_team(world, epic)
    child = world.child(leaf, parent=team.manager)
    pod = await world.admit(child, leaf, parent_team_id=team.binding.team_id)
    pod_tree = tree_of(pod)
    epic_tree = tree_of(team.binding)
    if diverged_parent:
        (epic_tree / "parent.txt").write_text("independent parent work\n")
        git_in(epic_tree, "add", "parent.txt")
        git_in(epic_tree, "commit", "-m", f"[gobby-#{epic.seq_num}] feat: advance parent")
    parent_tip = git_in(epic_tree, "rev-parse", "HEAD")
    (pod_tree / "delivered.txt").write_text("delivered\n")
    git_in(pod_tree, "add", "delivered.txt")
    git_in(pod_tree, "commit", "-m", f"[gobby-#{leaf.seq_num}] feat: deliver child")
    sha = git_in(pod_tree, "rev-parse", "HEAD")
    tasks = LocalTaskManager(world.case.db)
    tasks.link_commit(leaf.id, sha)
    leaf = tasks.get_task(leaf.id)
    main_tip = git_in(world.case.repo, "rev-parse", "HEAD")

    assert await task_landing_target(world.case.db, leaf) == (epic_tree, team.binding.branch)
    if dirty_parent:
        in_flight = epic_tree / "static-in-flight.txt"
        in_flight.write_text("unfinished static implementation\n")
        refused = await land_candidate(
            world.case.db, task=leaf, caller_session_id=team.manager, commit_sha=sha
        )
        assert refused.get("error") == "checkout_dirty", refused
        assert git_in(epic_tree, "rev-parse", "HEAD") == parent_tip
        assert in_flight.read_text() == "unfinished static implementation\n"
        in_flight.unlink()
    result = await land_candidate(
        world.case.db, task=leaf, caller_session_id=team.manager, commit_sha=sha
    )

    assert result.get("landed") is True, result
    landed_tip = git_in(epic_tree, "rev-parse", "HEAD")
    assert result["mode"] == ("merge" if diverged_parent else "ff")
    if diverged_parent:
        assert landed_tip not in {sha, parent_tip}
        assert git_in(epic_tree, "merge-base", "--is-ancestor", sha, landed_tip) == ""
        assert git_in(epic_tree, "merge-base", "--is-ancestor", parent_tip, landed_tip) == ""
    else:
        assert landed_tip == sha
    assert git_in(world.case.repo, "rev-parse", "HEAD") == main_tip
