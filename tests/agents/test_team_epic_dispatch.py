"""A team manager dispatches leaf developers and reviewers inside its epic (addendum 8.2)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.agents.team_lifecycle import TeamBinding
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
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
    synthetic_manager_body,
)

pytestmark = pytest.mark.unit

# make_team_world stubs the definition loader; these tests need the installed definitions.
_REAL_AGENT_BODY = _factory._load_agent_body
LEAF_TITLE = "Wire the scroll handler"
LONG_LEAF_TITLE = "Teach the terminal pane to keep its scroll position across reconnects"
LONG_LEAF_SLUG = "teach-the-terminal-pane-to-keep-its-scro"
# Definitions by their step grants; tech-writer grants nothing a team seat writes.
DEFINITIONS: dict[str, tuple[str, ...]] = {
    "developer": ("implementation",),
    "code-reviewer": ("review",),
    "docs-reviewer": ("review",),
    "research-reviewer": ("review",),
    "tech-writer": (),
}


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
            parent_session_id=world.case.creator,
            provider="claude",
            prompt=kwargs["prompt"],
            agent_name=kwargs["agent_lookup_name"],
            task_id=kwargs.get("task_id"),
        )
        runs.start(run.id)
        return {"success": True, "run_id": run.id}

    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)
    project_id = world.case.project_id
    definitions = AgentDefinitionManager(temp_db)
    for name, steps in DEFINITIONS.items():
        definitions.create(name, _body(name, steps), project_id=project_id)
    # A synthetic Dev Manager carries the team_manager grant and spawn scope under test.
    definitions.create("dev-manager", synthetic_manager_body(), project_id=project_id)
    return world


def _body(name: str, steps: tuple[str, ...]) -> dict[str, Any]:
    return {
        "name": name,
        "version": "1.0",
        "prompts": {"agent": f"Run the {name} seat."},
        "workflows": {"rule_selectors": {"include": []}},
        "task_step_writes": list(steps),
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


async def epic_team(world: TeamWorld, epic: Task) -> EpicTeam:
    """An admitted epic team with its running Dev Manager seat, which never claims."""
    db = world.case.db
    runbook = world.child(epic)
    binding = await world.admit(runbook, epic)
    manager = world.case.session("dev-manager", parent=runbook.id)
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent_session_id=runbook.id,
        provider="claude",
        prompt="Manage the epic team.",
        agent_name="dev-manager",
        child_session_id=manager,
        task_id=epic.id,
    )
    runs.start(run.id)
    SessionVariableManager(db).merge_variables(manager, {"_agent_type": "dev-manager"})
    SessionManager(db).update_terminal_pickup_metadata(manager, agent_run_id=run.id)
    return EpicTeam(world, epic, runbook, binding, manager)


def reviewer_bindings(team: EpicTeam) -> dict[str, str]:
    record = team.world.admit_step_output(team.runbook)["team"]
    return cast(dict[str, str], record.get("reviewers"))


def primary_bindings(team: EpicTeam) -> dict[str, str]:
    record = team.world.admit_step_output(team.runbook)["team"]
    return cast(dict[str, str], record.get("primaries"))


def launched_names(launch: dict[str, Any]) -> set[str]:
    """Every string, and every path's final name, the spawn passed to the launcher."""
    values = {value for value in launch.values() if isinstance(value, str)}
    return values | {Path(value).name for value in values}


async def delete_tree(
    world: TeamWorld, worktree_id: str, merged_into: str
) -> WorktreeDeletionResult:
    """The existing deletion path a Dev Manager uses for a finished sub-worktree."""
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
async def test_live_team_epic_survives_its_last_leaf_close(world: TeamWorld) -> None:
    """A live team's epic stays open when its last leaf closes (Dev Manager ruling).

    The manager holds no claim, so the parent-close cascade has nothing to stop at. The
    team's live binding must stop it, or closing the last leaf closes the bound epic
    before the epic tip is landed and its closing record written.
    """
    epic = new_task(world, "Keep the bound epic open", epic=True)
    leaf = new_task(world, "The only leaf", parent=epic)
    team = await epic_team(world, epic)
    tasks = LocalTaskManager(world.case.db)

    tasks.close_task(leaf.id, reason="moot", closed_in_session_id=team.manager)

    assert tasks.get_task(leaf.id).closed_at is not None
    assert tasks.get_task(epic.id).closed_at is None


@pytest.mark.asyncio
async def test_manager_leaf_spawns_scoped(world: TeamWorld) -> None:
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    long_leaf = new_task(world, LONG_LEAF_TITLE, parent=epic)
    busy = new_task(world, "Already in flight", parent=epic)
    team = await epic_team(world, epic)
    seq = epic.seq_num

    # A primary works in the epic worktree itself.
    primary = await team.spawn("developer", task_id=leaf.id, worktree_id=team.binding.worktree_id)
    assert primary.get("success") is True, primary
    assert world.launches[-1]["worktree_id"] == team.binding.worktree_id
    assert world.launches[-1].get("branch_name") is None

    # A sub-worktree developer gets its leaf branch and titled tree off epic main.
    sub = await team.spawn("developer", task_id=long_leaf.id, checkout_mode="worktree")
    assert sub.get("success") is True, sub
    launch = world.launches[-1]
    assert launch["branch_name"] == f"epic/{seq}/{long_leaf.seq_num}"
    assert launch["base_branch"] == f"epic/{seq}/main"
    assert f"wt-epic-{seq}-{long_leaf.seq_num}-{LONG_LEAF_SLUG}" in launched_names(launch)

    # Out of scope: another task, a definition without a team grant, an in-flight leaf.
    outside = new_task(world, "Outside the epic")
    in_flight_seat = world.case.session("developer", parent=team.runbook.id)
    LocalTaskManager(world.case.db).claim_task(busy.id, in_flight_seat, force=True)
    before = len(world.launches)
    for agent, target in (
        ("developer", {"task_id": outside.id, "checkout_mode": "worktree"}),
        ("tech-writer", {"task_id": leaf.id, "checkout_mode": "worktree"}),
        ("developer", {"task_id": busy.id, "checkout_mode": "worktree"}),
    ):
        reply = await team.spawn(agent, **target)
        assert reply.get("success") is not True, (agent, target, reply)
    assert len(world.launches) == before


@pytest.mark.asyncio
async def test_one_live_primary_per_implementation_domain(world: TeamWorld) -> None:
    """A second primary in a domain waits for the first; other domains and sub-worktrees do not."""
    epic = new_task(world, TITLE, epic=True)
    backend = new_task(world, LEAF_TITLE, parent=epic, domain="backend")
    second_backend = new_task(world, "Second backend leaf", parent=epic, domain="backend")
    frontend = new_task(world, "Frontend leaf", parent=epic, domain="frontend")
    team = await epic_team(world, epic)
    tree = team.binding.worktree_id

    first = await team.spawn("developer", task_id=backend.id, worktree_id=tree)
    assert first.get("success") is True, first
    refused = await team.spawn("developer", task_id=second_backend.id, worktree_id=tree)
    assert refused.get("error_code") == "primary_live", refused
    assert primary_bindings(team) == {"backend": first["run_id"]}

    other = await team.spawn("developer", task_id=frontend.id, worktree_id=tree)
    assert other.get("success") is True, other
    sub = await team.spawn("developer", task_id=second_backend.id, checkout_mode="worktree")
    assert sub.get("success") is True, sub
    assert primary_bindings(team) == {"backend": first["run_id"], "frontend": other["run_id"]}


@pytest.mark.asyncio
async def test_ended_primary_frees_its_domain_and_no_domain_is_fullstack(
    world: TeamWorld,
) -> None:
    """A leaf without a domain takes the fullstack slot; an ended primary frees its slot."""
    epic = new_task(world, TITLE, epic=True)
    undomained = new_task(world, LEAF_TITLE, parent=epic)
    fullstack = new_task(world, "Fullstack leaf", parent=epic, domain="fullstack")
    team = await epic_team(world, epic)
    tree = team.binding.worktree_id

    first = await team.spawn("developer", task_id=undomained.id, worktree_id=tree)
    assert first.get("success") is True, first
    refused = await team.spawn("developer", task_id=fullstack.id, worktree_id=tree)
    assert refused.get("error_code") == "primary_live", refused

    LocalAgentRunManager(world.case.db).complete(first["run_id"])
    replaced = await team.spawn("developer", task_id=fullstack.id, worktree_id=tree)
    assert replaced.get("success") is True, replaced
    assert primary_bindings(team) == {"fullstack": replaced["run_id"]}


@pytest.mark.asyncio
async def test_epic_manager_resumes_its_seat_outside_dispatch(world: TeamWorld) -> None:
    """A resume names a stored seat and reuses its tree, so dispatch takes no part in it."""
    db = world.case.db
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await epic_team(world, epic)
    seat = world.case.session("developer", parent=team.runbook.id)
    LocalAgentRunManager(db).create(
        parent_session_id=team.runbook.id,
        provider="claude",
        prompt="Implement the leaf.",
        agent_name="developer",
        child_session_id=seat,
        task_id=leaf.id,
    )
    SessionVariableManager(db).merge_variables(seat, {"_agent_type": "developer"})

    resumed = await team.spawn("developer", resume_session_id=seat)

    assert resumed.get("success") is True, resumed
    assert world.launches[-1]["resume_session_id"] == seat


def stored_reviewer_seat(world: TeamWorld, team: EpicTeam, agent: str) -> str:
    """An ended reviewer seat with a stored run, which a resume names."""
    seat = world.case.session(agent, parent=team.runbook.id)
    LocalAgentRunManager(world.case.db).create(
        parent_session_id=team.runbook.id,
        provider="claude",
        prompt="Review the team tree.",
        agent_name=agent,
        child_session_id=seat,
    )
    SessionVariableManager(world.case.db).merge_variables(seat, {"_agent_type": agent})
    return seat


def stored_primary_seat(world: TeamWorld, team: EpicTeam, leaf: Task, tree: str) -> str:
    """An ended developer seat whose run worked the leaf in the team tree, which a resume names."""
    seat = world.case.session("developer", parent=team.runbook.id)
    runs = LocalAgentRunManager(world.case.db)
    run = runs.create(
        parent_session_id=team.runbook.id,
        provider="claude",
        prompt="Work the leaf.",
        agent_name="developer",
        child_session_id=seat,
        task_id=leaf.id,
        worktree_id=tree,
    )
    runs.start(run.id)
    runs.complete(run.id)
    world.case.db.execute("UPDATE sessions SET agent_run_id = %s WHERE id = %s", (run.id, seat))
    SessionVariableManager(world.case.db).merge_variables(seat, {"_agent_type": "developer"})
    return seat


@pytest.mark.asyncio
async def test_resumed_reviewer_is_refused_beside_a_live_seat(world: TeamWorld) -> None:
    """A resume joins the team's one live seat per definition, as a fresh spawn does."""
    epic = new_task(world, TITLE, epic=True)
    new_task(world, "Code leaf", parent=epic)
    team = await epic_team(world, epic)
    live = await team.spawn("code-reviewer", worktree_id=team.binding.worktree_id)
    assert live.get("success") is True, live
    seat = stored_reviewer_seat(world, team, "code-reviewer")
    before = len(world.launches)

    resumed = await team.spawn("code-reviewer", resume_session_id=seat)

    assert resumed.get("success") is not True, resumed
    assert resumed.get("error_code") == "reviewer_seat_live"
    assert len(world.launches) == before
    assert reviewer_bindings(team) == {"code-reviewer": live["run_id"]}


@pytest.mark.asyncio
async def test_resumed_reviewer_takes_its_definitions_binding(world: TeamWorld) -> None:
    """A resumed reviewer's new run replaces the ended one in its definition's binding."""
    epic = new_task(world, TITLE, epic=True)
    new_task(world, "Code leaf", parent=epic)
    team = await epic_team(world, epic)
    fresh = await team.spawn("code-reviewer", worktree_id=team.binding.worktree_id)
    assert fresh.get("success") is True, fresh
    LocalAgentRunManager(world.case.db).complete(fresh["run_id"])
    seat = stored_reviewer_seat(world, team, "code-reviewer")

    resumed = await team.spawn("code-reviewer", resume_session_id=seat)

    assert resumed.get("success") is True, resumed
    assert reviewer_bindings(team) == {"code-reviewer": resumed["run_id"]}


@pytest.mark.asyncio
async def test_resumed_reviewer_keeps_its_stored_definition(world: TeamWorld) -> None:
    """A resume may not retype a reviewer seat onto another reviewer definition."""
    epic = new_task(world, TITLE, epic=True)
    new_task(world, "Code leaf", parent=epic)
    team = await epic_team(world, epic)
    seat = stored_reviewer_seat(world, team, "code-reviewer")
    before = len(world.launches)

    resumed = await team.spawn("docs-reviewer", resume_session_id=seat)

    assert resumed.get("success") is not True, resumed
    assert resumed.get("error_code") == "seat_definition_mismatch"
    assert len(world.launches) == before
    assert reviewer_bindings(team) == {}


@pytest.mark.asyncio
async def test_resumed_reviewer_cannot_be_retyped_as_a_maker(world: TeamWorld) -> None:
    """A bound Dev Manager may not resume a reviewer seat as a developer."""
    epic = new_task(world, TITLE, epic=True)
    new_task(world, LEAF_TITLE, parent=epic)
    team = await epic_team(world, epic)
    seat = stored_reviewer_seat(world, team, "code-reviewer")
    before = len(world.launches)

    resumed = await team.spawn("developer", resume_session_id=seat)

    assert resumed.get("error_code") == "seat_definition_mismatch", resumed
    assert len(world.launches) == before
    assert reviewer_bindings(team) == {}
    assert primary_bindings(team) == {}


@pytest.mark.asyncio
async def test_resumed_primary_takes_its_domain_slot(world: TeamWorld) -> None:
    """A resumed primary rebinds its implementation domain when no live primary holds it."""
    epic = new_task(world, TITLE, epic=True)
    backend = new_task(world, LEAF_TITLE, parent=epic, domain="backend")
    team = await epic_team(world, epic)
    seat = stored_primary_seat(world, team, backend, team.binding.worktree_id)

    resumed = await team.spawn("developer", resume_session_id=seat)

    assert resumed.get("success") is True, resumed
    assert primary_bindings(team) == {"backend": resumed["run_id"]}


@pytest.mark.asyncio
async def test_resumed_primary_refused_beside_a_live_domain_primary(world: TeamWorld) -> None:
    """A resume may not add a second live primary to a domain another seat holds."""
    epic = new_task(world, TITLE, epic=True)
    backend = new_task(world, LEAF_TITLE, parent=epic, domain="backend")
    second_backend = new_task(world, "Second backend leaf", parent=epic, domain="backend")
    team = await epic_team(world, epic)
    tree = team.binding.worktree_id
    seat = stored_primary_seat(world, team, backend, tree)
    live = await team.spawn("developer", task_id=second_backend.id, worktree_id=tree)
    assert live.get("success") is True, live
    before = len(world.launches)

    resumed = await team.spawn("developer", resume_session_id=seat)

    assert resumed.get("error_code") == "primary_live", resumed
    assert len(world.launches) == before
    assert primary_bindings(team) == {"backend": live["run_id"]}


@pytest.mark.asyncio
async def test_single_team_manager_spawns_no_fresh_seat(world: TeamWorld) -> None:
    """A single-task team runs the redesign flow: its runbook starts the seats, and its manager
    only resumes them. A fresh spawn from its manager is refused, never launched."""
    db = world.case.db
    task = new_task(world, "Single task")
    runbook = world.child(task)
    await world.admit(runbook, task)
    manager = world.case.session("dev-manager", parent=runbook.id)
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent_session_id=runbook.id,
        provider="claude",
        prompt="Manage the task.",
        agent_name="dev-manager",
        child_session_id=manager,
        task_id=task.id,
    )
    runs.start(run.id)
    SessionVariableManager(db).merge_variables(manager, {"_agent_type": "dev-manager"})
    SessionManager(db).update_terminal_pickup_metadata(manager, agent_run_id=run.id)
    launches = len(world.launches)

    refused = await world.direct_spawn(manager, agent="developer", task_id=task.id)

    assert refused.get("error_code") == "team_manager_scope", refused
    assert len(world.launches) == launches


@pytest.mark.asyncio
async def test_merged_leaf_tree_cleanup(world: TeamWorld) -> None:
    db = world.case.db
    epic = new_task(world, TITLE, epic=True)
    merged_leaf = new_task(world, LEAF_TITLE, parent=epic)
    open_leaf = new_task(world, "Unmerged leaf", parent=epic)
    team = await epic_team(world, epic)
    epic_tree = Path(team.binding.worktree_path)
    trees = LocalWorktreeManager(db)

    async def sub_tree(leaf: Task) -> tuple[str, Path, str]:
        """Dispatch a sub-worktree developer, then make the tree its launch named."""
        reply = await team.spawn("developer", task_id=leaf.id, checkout_mode="worktree")
        assert reply.get("success") is True, reply
        branch = world.launches[-1]["branch_name"]
        assert branch == f"epic/{epic.seq_num}/{leaf.seq_num}"
        path = epic_tree.parent / f"sub-{epic.seq_num}-{leaf.seq_num}"
        git_in(epic_tree, "worktree", "add", "-b", branch, str(path), team.binding.branch)
        row = trees.create(
            world.case.project_id,
            branch_name=branch,
            worktree_path=str(path),
            base_branch=team.binding.branch,
            task_id=leaf.id,
        )
        (path / f"leaf_{leaf.seq_num}.py").write_text("work = True\n")
        git_in(path, "add", "--all")
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


@pytest.mark.asyncio
async def test_routed_reviewer_bindings(world: TeamWorld) -> None:
    epic = new_task(world, TITLE, epic=True)
    new_task(world, "Code leaf", parent=epic)
    new_task(world, "Guide leaf", parent=epic)
    new_task(world, "Research leaf", parent=epic)
    team = await epic_team(world, epic)
    runs = LocalAgentRunManager(world.case.db)
    tree = {"worktree_id": team.binding.worktree_id}

    spawned: dict[str, str] = {}
    for reviewer in ("code-reviewer", "docs-reviewer", "research-reviewer"):
        reply = await team.spawn(reviewer, **tree)
        assert reply.get("success") is True, (reviewer, reply)
        assert world.launches[-1]["worktree_id"] == team.binding.worktree_id
        spawned[reviewer] = reply["run_id"]
    assert reviewer_bindings(team) == spawned

    # A second live seat for a bound definition, and a definition without the grant, are refused.
    before = len(world.launches)
    for agent in ("docs-reviewer", "tech-writer"):
        reply = await team.spawn(agent, **tree)
        assert reply.get("success") is not True, (agent, reply)
    assert len(world.launches) == before
    assert reviewer_bindings(team) == spawned

    # An ended reviewer is replaced in its own definition's binding.
    runs.complete(spawned["docs-reviewer"])
    replacement = await team.spawn("docs-reviewer", **tree)
    assert replacement.get("success") is True, replacement
    assert reviewer_bindings(team) == {**spawned, "docs-reviewer": replacement["run_id"]}
