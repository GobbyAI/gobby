"""Epic primary leaves and pods use the single-task review and landing lifecycle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby.agents.team_dispatch import dispatch_for_caller
from gobby.agents.team_lifecycle import (
    TeamAdmissionRefused,
    TeamBinding,
    bind_reviewer_run,
    managed_task_team,
    seat_team,
)
from gobby.agents.team_review import seat_team_held, sync_epic_review
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close import _evaluate_close
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.workspaces import WorkspaceManager
from gobby.tasks.land_commit import land_candidate, task_landing_target
from gobby.tasks.step_records import (
    NoWorkDisposition,
    StepRecordError,
    StepStatus,
    StepSubmission,
    TaskStep,
    approved_task_step,
    list_task_step_records,
    record_task_step,
)
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.condition_helpers_sessions import (
    dev_pod_launch_allowed,
    own_seat_resume,
    own_team_tab,
)
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.review_approvals import grant_step_writes
from tests.fixtures.team_case import (
    TITLE,
    TeamWorld,
    git_in,
    make_team_world,
    running_seat,
    synthetic_manager_body,
)

pytestmark = pytest.mark.unit

# make_team_world stubs the definition loader; the held-spawn checks need installed definitions.
_REAL_AGENT_BODY = _factory._load_agent_body


def tree_of(binding: TeamBinding) -> Path:
    """The team tree a binding names; these tests always admit a team with one."""
    assert binding.worktree_path is not None
    return Path(binding.worktree_path)


@dataclass
class EpicTeam:
    world: TeamWorld
    epic: Task
    runbook: Session
    binding: TeamBinding
    manager: str

    @property
    def db(self) -> HubDatabase:
        return self.world.case.db

    def seat(self, definition: str, step: TaskStep) -> str:
        """A team seat launched by the runbook, running a definition that writes ``step``."""
        session = self.world.case.session(definition, parent=self.runbook.id)
        LocalAgentRunManager(self.db).create(
            parent_session_id=self.runbook.id,
            provider="claude",
            prompt=f"Run the {definition} seat.",
            agent_name=definition,
            child_session_id=session,
        )
        grant_step_writes(self.db, session, self.world.case.project_id, definition, (step,))
        return session

    def commit(self, task: Task, name: str) -> str:
        """Commit ``name`` in the epic tree and link it as ``task``'s latest commit."""
        tree = tree_of(self.binding)
        (tree / name).write_text(f"# {name}\n")
        git_in(tree, "add", name)
        git_in(tree, "commit", "-m", f"[gobby-#{task.seq_num}] feat: {name}")
        sha = git_in(tree, "rev-parse", "HEAD")
        link(self.db, task, sha)
        return sha

    def link_tip(self) -> str:
        """Link the epic tree's tip to the epic, as the Dev Manager does before the epic review."""
        sha = git_in(tree_of(self.binding), "rev-parse", "HEAD")
        link(self.db, self.epic, sha)
        return sha


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

    async def launch(**kwargs: Any) -> dict[str, Any]:
        world.launches.append(kwargs)
        return {"success": True, "run_id": kwargs.get("reserved_run_id")}

    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)
    definitions = AgentDefinitionManager(temp_db)
    project_id = world.case.project_id
    definitions.create("dev-manager", synthetic_manager_body(), project_id=project_id)
    definitions.create(
        "developer",
        {
            "name": "developer",
            "version": "1.0",
            "prompts": {"agent": "Implement the leaf."},
            "workflows": {"rule_selectors": {"include": []}},
            "task_step_writes": ["implementation"],
        },
        project_id=project_id,
    )
    return world


def link(db: HubDatabase, task: Task, sha: str) -> None:
    commits = LocalTaskManager(db).get_task(task.id).commits or []
    db.execute(
        "UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([*commits, sha]), task.id)
    )


def new_task(
    world: TeamWorld, title: str, *, parent: Task | None = None, epic: bool = False
) -> Task:
    return LocalTaskManager(world.case.db).create_task(
        world.case.project_id,
        title,
        parent_task_id=None if parent is None else parent.id,
        created_in_session_id=world.case.creator,
        task_type="epic" if epic else "task",
        validation_criteria=None if epic else "The leaf works.",
    )


async def epic_team(world: TeamWorld, epic: Task) -> EpicTeam:
    """An admitted epic team with a running Dev Manager seat. The manager never claims the epic."""
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


def submit(
    db: HubDatabase,
    session_id: str,
    task: Task,
    step: TaskStep,
    status: StepStatus,
    round_number: int,
    sha: str,
) -> None:
    record_task_step(
        db,
        task_id=task.id,
        session_id=session_id,
        step=step,
        submission=StepSubmission(
            status=status,
            epoch=LocalTaskManager(db).get_task(task.id).review_epoch,
            round_number=round_number,
            feedback=f"{step} {status} in round {round_number}",
            commit_sha=sha,
        ),
    )


def reject_rounds(team: EpicTeam, leaf: Task, developer: str, reviewer: str, rounds: int) -> str:
    """The developer implements and the reviewer rejects, ``rounds`` times."""
    sha = ""
    for number in range(1, rounds + 1):
        sha = team.commit(leaf, f"leaf_{leaf.seq_num}_round_{number}.py")
        submit(team.db, developer, leaf, "implementation", "approved", number, sha)
        submit(team.db, reviewer, leaf, "review", "rejected", number, sha)
    return sha


def approved(db: HubDatabase, task: Task, step: TaskStep) -> bool:
    current = LocalTaskManager(db).get_task(task.id)
    return approved_task_step(current, list_task_step_records(db, task.id), step) is not None


async def hand_off(
    tools: InternalToolRegistry, team: EpicTeam, leaf: Task, holder: str, recipient: str
) -> dict[str, Any]:
    arguments = {
        "task_id": leaf.id,
        "holder_session_ref": holder,
        "recipient_session_ref": recipient,
        "reason": "Return the rejected leaf to a developer",
    }
    with session_context_for_test(team.manager):
        return cast(dict[str, Any], await tools.call("hand_off_task_claim", arguments))


@pytest.mark.asyncio
async def test_epic_leaf_uses_single_task_fixer_and_fresh_reviewer(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    tools = create_task_registry(tasks)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, "Code leaf", parent=epic)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    tasks.claim_task(leaf.id, developer, force=True)
    sha = reject_rounds(team, leaf, developer, reviewer, 2)

    moved = await hand_off(tools, team, leaf, developer, reviewer)
    assert moved.get("success") is True, moved
    assert tasks.get_task(leaf.id).claimed_by_session_id == reviewer
    submit(team.db, reviewer, leaf, "implementation", "approved", 3, sha)
    with pytest.raises(StepRecordError, match="claimant"):
        submit(team.db, reviewer, leaf, "review", "approved", 3, sha)

    fresh = world.child(leaf)
    admitted = await world.admit(
        fresh, leaf, team.binding.team_id, fresh_reviewer=True, exclude_reviewers=[reviewer]
    )
    assert admitted == team.binding
    assert (
        await world.admit(
            fresh, leaf, team.binding.team_id, fresh_reviewer=True, exclude_reviewers=[reviewer]
        )
        == admitted
    )

    # The definition that wrote the leaf's earlier reviews reviews its next round.
    state = sync_epic_review(team.db, team.binding.team_id)
    assert state.next_reviewers == {leaf.id: "code-reviewer"}
    assert (state.held, state.holds) == (False, ())

    sibling = new_task(world, "Different leaf", parent=epic)
    with pytest.raises(TeamAdmissionRefused) as refused:
        await world.admit(fresh, sibling, team.binding.team_id, fresh_reviewer=True)
    assert refused.value.code == "team_binding_drift"

    # A separate leaf at the same review epoch gets its own fresh-review reservation.
    tasks.claim_task(sibling.id, developer, force=True)
    reject_rounds(team, sibling, developer, reviewer, 2)
    assert (await hand_off(tools, team, sibling, developer, reviewer)).get("success") is True
    other_fresh = world.child(sibling)
    assert (
        await world.admit(
            other_fresh,
            sibling,
            team.binding.team_id,
            fresh_reviewer=True,
            exclude_reviewers=[reviewer],
        )
        == team.binding
    )


@pytest.mark.asyncio
async def test_epic_manager_admits_isolated_leaf_pod(world: TeamWorld) -> None:
    epic = new_task(world, "Parallel delivery", epic=True)
    leaf = new_task(world, "Independent leaf", parent=epic)
    sibling = new_task(world, "Other leaf", parent=epic)
    team = await epic_team(world, epic)
    run = world.child(leaf, parent=team.manager)
    pod = await world.admit(run, leaf, parent_team_id=team.binding.team_id)

    assert pod.mode == "single"
    assert pod.parent_team_id == team.binding.team_id
    assert pod.branch == f"epic/{epic.seq_num}/{leaf.seq_num}"
    assert pod.base_branch == team.binding.branch
    assert pod.worktree_id != team.binding.worktree_id
    assert git_in(tree_of(pod), "rev-parse", "HEAD") == git_in(
        tree_of(team.binding), "rev-parse", "HEAD"
    )
    assert managed_task_team(team.db, team.manager, leaf.id) == pod
    assert await world.admit(run, leaf, parent_team_id=team.binding.team_id) == pod

    with pytest.raises(TeamAdmissionRefused) as refused:
        await world.admit(world.child(leaf), leaf, team.binding.team_id)
    assert refused.value.code == "team_task_mismatch"

    writer = running_seat(world, run, "test-writer")
    tools = create_task_registry(LocalTaskManager(team.db))
    with session_context_for_test(writer.id):
        claimed = await tools.call("claim_task", {"task_id": leaf.id})
        wrong_leaf = await tools.call("claim_task", {"task_id": sibling.id})
    assert claimed.get("success") is True, claimed
    assert wrong_leaf.get("error_code") == "TASK_CLAIM_CONFLICT", wrong_leaf
    developer = running_seat(world, run, "developer")
    moved = await hand_off(tools, team, leaf, writer.id, developer.id)
    assert moved.get("success") is True, moved


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_pod_waits_for_unresolved_dependency(world: TeamWorld, external: bool) -> None:
    epic = new_task(world, "Dependency-ordered pods", epic=True)
    leaf = new_task(world, "Dependent leaf", parent=epic)
    prerequisite = new_task(world, "Prerequisite", parent=None if external else epic)
    TaskDependencyManager(world.case.db).add_dependency(leaf.id, prerequisite.id, "blocks")
    team = await epic_team(world, epic)
    run = world.child(leaf, parent=team.manager)
    inputs = {"parent_team_id": team.binding.team_id, "task": leaf.id}
    sessions = SessionManager(team.db)

    assert not dev_pod_launch_allowed(sessions, team.manager, "pipeline:devteam", inputs)
    with pytest.raises(TeamAdmissionRefused) as refused:
        await world.admit(run, leaf, parent_team_id=team.binding.team_id)
    assert refused.value.code == "pod_leaf_blocked"
    assert managed_task_team(team.db, team.manager, leaf.id) == team.binding

    LocalTaskManager(team.db).close_task(prerequisite.id, reason="moot")
    assert dev_pod_launch_allowed(sessions, team.manager, "pipeline:devteam", inputs)
    pod = await world.admit(run, leaf, parent_team_id=team.binding.team_id)
    assert pod.parent_team_id == team.binding.team_id
    assert pod.worktree_id != team.binding.worktree_id


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_epic_hold", [False, True])
async def test_pod_admission_respects_only_explicit_epic_hold(
    world: TeamWorld, explicit_epic_hold: bool
) -> None:
    epic = new_task(world, "Held epic", epic=True)
    leaf = new_task(world, "Ready leaf", parent=epic)
    sibling = new_task(world, "Stuck sibling", parent=epic)
    team = await epic_team(world, epic)
    tasks = LocalTaskManager(team.db)
    tasks.escalate_task(epic.id if explicit_epic_hold else sibling.id, "Operator hold")
    run = world.child(leaf, parent=team.manager)
    if explicit_epic_hold:
        with pytest.raises(TeamAdmissionRefused) as refused:
            await world.admit(run, leaf, parent_team_id=team.binding.team_id)
        assert refused.value.code == "team_held"
        tasks.de_escalate_task(epic.id, reason="Operator released hold")
    pod = await world.admit(run, leaf, parent_team_id=team.binding.team_id)
    assert pod.parent_team_id == team.binding.team_id
    assert not sync_epic_review(team.db, team.binding.team_id).held


@pytest.mark.asyncio
@pytest.mark.parametrize("depends_on_unstarted", [False, True])
async def test_ready_epic_tip_checks_merged_leaves_and_their_dependencies(
    world: TeamWorld, depends_on_unstarted: bool
) -> None:
    tasks = LocalTaskManager(world.case.db)
    epic = new_task(world, "Partial epic delivery", epic=True)
    first = new_task(world, "Merged first", parent=epic)
    second = new_task(world, "Merged second", parent=epic)
    unstarted = new_task(world, "Unstarted sibling", parent=epic)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    for leaf in (first, second):
        tasks.claim_task(leaf.id, developer, force=True)
        sha = team.commit(leaf, f"merged_{leaf.seq_num}.py")
        submit(team.db, developer, leaf, "implementation", "approved", 1, sha)
        submit(team.db, reviewer, leaf, "review", "approved", 1, sha)
    if depends_on_unstarted:
        TaskDependencyManager(team.db).add_dependency(second.id, unstarted.id, "blocks")
    team.link_tip()
    state = sync_epic_review(team.db, team.binding.team_id)
    assert state.held is False
    assert state.epic_tip_ready is not depends_on_unstarted
    assert set(state.holds) == ({second.id, unstarted.id} if depends_on_unstarted else set())

    tasks.escalate_task(unstarted.id, "Leaf needs operator decision")
    state = sync_epic_review(team.db, team.binding.team_id)
    assert not state.held
    assert tasks.get_task(epic.id).escalated_at is None
    assert state.epic_tip_ready is not depends_on_unstarted
    assert unstarted.id in state.holds
    assert not seat_team_held(team.db, team.binding)


@pytest.mark.asyncio
async def test_ready_tip_uses_included_approval_and_requires_git_evidence(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = LocalTaskManager(world.case.db)
    epic = new_task(world, "Included approvals", epic=True)
    leaf = new_task(world, "Leaf with later work", parent=epic)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    tasks.claim_task(leaf.id, developer, force=True)
    included = team.commit(leaf, "included.py")
    submit(team.db, developer, leaf, "implementation", "approved", 1, included)
    submit(team.db, reviewer, leaf, "review", "approved", 1, included)
    team.link_tip()
    later = team.commit(leaf, "later.py")
    submit(team.db, developer, leaf, "implementation", "approved", 2, later)
    submit(team.db, reviewer, leaf, "review", "rejected", 1, later)
    assert sync_epic_review(team.db, team.binding.team_id).epic_tip_ready

    team.link_tip()
    assert not sync_epic_review(team.db, team.binding.team_id).epic_tip_ready
    submit(team.db, reviewer, leaf, "review", "approved", 2, later)
    assert sync_epic_review(team.db, team.binding.team_id).epic_tip_ready

    monkeypatch.setattr("gobby.agents.team_review.run_git_command", lambda *args, **kwargs: None)
    assert not sync_epic_review(team.db, team.binding.team_id).epic_tip_ready


@pytest.mark.asyncio
async def test_tip_dependency_holds_propagate_without_holding_siblings(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    epic = new_task(world, "Transitive prerequisites", epic=True)
    first = new_task(world, "First", parent=epic)
    second = new_task(world, "Second", parent=epic)
    missing = new_task(world, "Missing prerequisite", parent=epic)
    sibling = new_task(world, "Independent sibling", parent=epic)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    for leaf in (first, second, sibling):
        tasks.claim_task(leaf.id, developer, force=True)
        sha = team.commit(leaf, f"leaf_{leaf.seq_num}.py")
        submit(team.db, developer, leaf, "implementation", "approved", 1, sha)
        submit(team.db, reviewer, leaf, "review", "approved", 1, sha)
    dependencies = TaskDependencyManager(team.db)
    dependencies.add_dependency(second.id, first.id, "blocks")
    dependencies.add_dependency(first.id, missing.id, "blocks")
    team.link_tip()
    state = sync_epic_review(team.db, team.binding.team_id)
    assert not state.held
    assert not state.epic_tip_ready
    assert set(state.holds) == {first.id, second.id, missing.id}
    assert sibling.id not in state.holds


@pytest.mark.asyncio
@pytest.mark.parametrize("nested_phase", [False, True])
async def test_closed_prerequisite_must_be_in_the_linked_tip(
    world: TeamWorld, nested_phase: bool
) -> None:
    tasks = LocalTaskManager(world.case.db)
    epic = new_task(world, "Closed prerequisites", epic=True)
    dependent = new_task(world, "Dependent", parent=epic)
    phase = new_task(world, "Prerequisite phase", epic=True, parent=epic) if nested_phase else epic
    prerequisite = new_task(world, "Prerequisite closed after tip linked", parent=phase)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    for leaf in (dependent, prerequisite):
        tasks.claim_task(leaf.id, developer, force=True)
        sha = team.commit(leaf, f"leaf_{leaf.seq_num}.py")
        submit(team.db, developer, leaf, "implementation", "approved", 1, sha)
        submit(team.db, reviewer, leaf, "review", "approved", 1, sha)
        if leaf.id == dependent.id:
            team.link_tip()
    TaskDependencyManager(team.db).add_dependency(
        dependent.id, phase.id if nested_phase else prerequisite.id, "blocks"
    )
    tasks.close_task(prerequisite.id, reason="done")
    if nested_phase:
        assert tasks.get_task(phase.id).closed_at is not None

    stale = sync_epic_review(team.db, team.binding.team_id)
    assert not stale.held
    assert not stale.epic_tip_ready
    expected_holds = {dependent.id, prerequisite.id}
    if nested_phase:
        expected_holds.add(phase.id)
    assert set(stale.holds) == expected_holds
    team.link_tip()
    current = sync_epic_review(team.db, team.binding.team_id)
    assert current.epic_tip_ready
    assert current.holds == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["moot", "superseded", "wont_do"])
@pytest.mark.parametrize("nested_phase", [False, True])
async def test_no_work_prerequisite_satisfies_linked_tip(
    world: TeamWorld, disposition: NoWorkDisposition, nested_phase: bool
) -> None:
    tasks = LocalTaskManager(world.case.db)
    epic = new_task(world, "No-work prerequisites", epic=True)
    dependent = new_task(world, "Dependent", parent=epic)
    phase = new_task(world, "No-work phase", epic=True, parent=epic) if nested_phase else epic
    prerequisite = new_task(world, "Unnecessary implementation", parent=phase)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    tasks.claim_task(dependent.id, developer, force=True)
    sha = team.commit(dependent, "dependent.py")
    submit(team.db, developer, dependent, "implementation", "approved", 1, sha)
    submit(team.db, reviewer, dependent, "review", "approved", 1, sha)
    team.link_tip()
    TaskDependencyManager(team.db).add_dependency(
        dependent.id, phase.id if nested_phase else prerequisite.id, "blocks"
    )
    expected_holds = {dependent.id, prerequisite.id}
    if nested_phase:
        expected_holds.add(phase.id)
    statuses: tuple[StepStatus, ...] = ("rejected", "approved")
    for round_number, status in enumerate(statuses, start=1):
        record_task_step(
            team.db,
            task_id=prerequisite.id,
            session_id=reviewer,
            step="review",
            submission=StepSubmission(
                status=status,
                epoch=prerequisite.review_epoch,
                round_number=round_number,
                feedback=f"No-work disposition {status}",
                disposition=disposition,
            ),
        )
        state = sync_epic_review(team.db, team.binding.team_id)
        assert state.epic_tip_ready is (status == "approved")
        assert set(state.holds) == (set() if status == "approved" else expected_holds)

    # The real no-work path has review evidence only, then an operator close.
    records = list_task_step_records(team.db, prerequisite.id)
    assert {record.step for record in records} == {"review"}
    with session_context_for_test(world.case.creator):
        closed = await create_task_registry(tasks).call(
            "close_task",
            {
                "task_id": prerequisite.id,
                "reason": disposition,
                **({"superseded_by": dependent.id} if disposition == "superseded" else {}),
            },
        )
    assert closed.get("closed") is True, closed
    state = sync_epic_review(team.db, team.binding.team_id)
    assert state.epic_tip_ready
    assert state.holds == ()
    assert tasks.get_task(prerequisite.id).closed_at is not None


@pytest.mark.asyncio
async def test_running_pod_review_respects_explicit_parent_hold(world: TeamWorld) -> None:
    epic = new_task(world, "Parent hold", epic=True)
    leaf = new_task(world, "Running pod", parent=epic)
    team = await epic_team(world, epic)
    run = world.child(leaf, parent=team.manager)
    pod = await world.admit(run, leaf, parent_team_id=team.binding.team_id)
    leaf_team = EpicTeam(world, leaf, run, pod, team.manager)
    developer = leaf_team.seat("developer", "implementation")
    reviewer = leaf_team.seat("code-reviewer", "review")
    tasks = LocalTaskManager(team.db)
    tasks.claim_task(leaf.id, developer, force=True)
    sha = leaf_team.commit(leaf, "pod_work.py")
    tasks.escalate_task(epic.id, "Operator hold")
    submit(team.db, developer, leaf, "implementation", "approved", 1, sha)
    with pytest.raises(StepRecordError, match="held"):
        submit(team.db, reviewer, leaf, "review", "approved", 1, sha)
    tasks.de_escalate_task(epic.id, reason="Operator released hold")
    submit(team.db, reviewer, leaf, "review", "approved", 1, sha)
    assert approved(team.db, tasks.get_task(leaf.id), "review")


@pytest.mark.asyncio
async def test_pod_rejection_two_admits_fresh_reviewer(world: TeamWorld) -> None:
    epic = new_task(world, "Pod reviewer recovery", epic=True)
    leaf = new_task(world, "Pod leaf", parent=epic)
    team = await epic_team(world, epic)
    run = world.child(leaf, parent=team.manager)
    pod = await world.admit(run, leaf, parent_team_id=team.binding.team_id)
    leaf_team = EpicTeam(world, leaf, run, pod, team.manager)
    developer = leaf_team.seat("developer", "implementation")
    reviewer = leaf_team.seat("code-reviewer", "review")
    tasks = LocalTaskManager(team.db)
    tasks.claim_task(leaf.id, developer, force=True)
    reject_rounds(leaf_team, leaf, developer, reviewer, 2)
    moved = await hand_off(create_task_registry(tasks), team, leaf, developer, reviewer)
    assert moved.get("success") is True, moved
    fresh = world.child(leaf)
    admitted = await world.admit(
        fresh, leaf, pod.team_id, fresh_reviewer=True, exclude_reviewers=[reviewer]
    )
    assert admitted == pod
    assert admitted.parent_team_id == team.binding.team_id
    assert admitted.worktree_id != team.binding.worktree_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent,grant",
    [
        ("test-writer", "test"),
        ("developer", "implementation"),
        ("code-reviewer", "review"),
        ("docs-reviewer", "review"),
        ("research-reviewer", "review"),
        ("researcher", "closing"),
    ],
)
async def test_epic_manager_resumes_pod_seat_in_its_own_team(
    world: TeamWorld, agent: str, grant: TaskStep
) -> None:
    epic = new_task(world, "Resume pod", epic=True)
    leaf = new_task(world, "Pod leaf", parent=epic)
    team = await epic_team(world, epic)
    run = world.child(leaf, parent=team.manager)
    pod = await world.admit(run, leaf, parent_team_id=team.binding.team_id)
    seat = running_seat(world, run, agent)
    primary = running_seat(world, team.runbook, "code-reviewer")
    assert primary.agent_run_id is not None
    bind_reviewer_run(
        team.db,
        team.binding.team_id,
        team.binding.project_id,
        "code-reviewer",
        primary.agent_run_id,
    )
    sessions = SessionManager(team.db)
    assert own_seat_resume(sessions, team.manager, agent, seat.id)
    assert own_team_tab(sessions, team.manager, agent) == f"Dev Team - #{epic.seq_num}"
    assert not own_seat_resume(sessions, team.manager, "default", seat.id)
    other = await epic_team(world, new_task(world, "Other epic", epic=True))
    assert not own_seat_resume(sessions, other.manager, agent, seat.id)
    body = AgentDefinitionBody.model_validate(
        {
            "name": agent,
            "version": "1.0",
            "task_step_writes": [grant],
            "prompts": {"agent": "Resume the assigned seat."},
            "workflows": {"rule_selectors": {"include": []}},
        }
    )
    scope = dispatch_for_caller(
        team.db,
        team.manager,
        agent_body=body,
        agent=agent,
        task_id=None,
        checkout_mode=None,
        branch_name=None,
        base_branch=None,
        worktree_id=None,
        resume_session_id=seat.id,
    )
    assert scope is not None and scope.team_id == pod.team_id
    assert scope.worktree_id is None  # Resume retains the seat's recorded tree.
    runs = LocalAgentRunManager(team.db)
    resumed = runs.create(
        parent_session_id=team.manager,
        provider="claude",
        prompt="Resume the pod seat.",
        agent_name=agent,
        child_session_id=seat.id,
        worktree_id=pod.worktree_id,
        resume_metadata_json={"resume_previous_run_id": seat.agent_run_id},
    )
    sessions.update_parent_session_id(seat.id, team.manager)
    sessions.update_terminal_pickup_metadata(seat.id, agent_run_id=resumed.id)
    assert seat_team(team.db, seat.id) == pod
    assert own_seat_resume(sessions, team.manager, agent, seat.id)


@pytest.mark.asyncio
async def test_pod_pipeline_launches_triplet_and_relaunches_in_pod_tab(world: TeamWorld) -> None:
    from tests.agents.test_devteam_rerun import (
        DEVTEAM,
        RENDERER,
        _admit,
        _inputs,
        _launched,
        _seat_prompt,
        _step_context,
    )

    epic = new_task(world, "Pipeline epic", epic=True)
    leaf = new_task(world, "Pipeline leaf", parent=epic)
    team = await epic_team(world, epic)
    definitions = AgentDefinitionManager(team.db)
    for name in ("test-writer", "code-reviewer"):
        definitions.create(name, {"name": name, "version": "1.0"}, project_id=world.case.project_id)
    workspace = WorkspaceManager(team.db).create(require_machine_id(), "devteam")[0].id
    inputs = _inputs(leaf, workspace, team.manager, None)
    inputs["parent_team_id"] = team.binding.team_id
    run = world.child(leaf, DEVTEAM, parent=team.manager)
    context = await _admit(world, run, inputs)
    assert _launched(run, context) == ["test-writer", "developer", "code-reviewer"]
    assert context["steps"]["guard"]["output"]["tab_title"] == f"Dev Pod - #{leaf.seq_num}"
    for name in ("test-writer", "developer", "code-reviewer"):
        assert team.manager in _seat_prompt(run, name, context)
    roster = next(step for step in DEVTEAM.steps if step.id == "roster")
    assert roster.mcp is not None
    context["steps"].update(
        {
            name: {"output": {"child_session_id": name, "run_id": name}}
            for name in ("test-writer", "developer", "code-reviewer")
        }
    )
    args = RENDERER.render_mcp_arguments(
        roster.mcp.arguments or {}, _step_context(run, roster.id, context), drop_none=True
    )
    assert args["target_id"] == team.manager
    world.finish(run)
    pod_id = context["steps"]["admit"]["output"]["team"]["team_id"]
    replay_inputs = _inputs(leaf, workspace, team.manager, pod_id)
    replacement = world.child(leaf, DEVTEAM, parent=team.manager)
    replay = await _admit(world, replacement, replay_inputs)
    assert _launched(replacement, replay) == ["test-writer", "developer", "code-reviewer"]
    assert replay["steps"]["guard"]["output"]["live"]["dev-manager"] == team.manager
    assert replay["steps"]["guard"]["output"]["tab_title"] == f"Dev Pod - #{leaf.seq_num}"


@pytest.mark.asyncio
async def test_only_epic_manager_can_launch_pods(world: TeamWorld) -> None:
    epic = new_task(world, "Managed epic", epic=True)
    leaf = new_task(world, "Open leaf", parent=epic)
    team = await epic_team(world, epic)
    inputs = {"parent_team_id": team.binding.team_id, "task": leaf.id}
    sessions = SessionManager(team.db)
    assert dev_pod_launch_allowed(
        sessions, team.manager, "run_pipeline", {"name": "devteam", "inputs": inputs}
    )
    assert dev_pod_launch_allowed(sessions, team.manager, "pipeline:devteam", inputs)
    assert not dev_pod_launch_allowed(
        sessions, team.manager, "run_pipeline", {"name": "other", "inputs": inputs}
    )
    assert not dev_pod_launch_allowed(
        sessions, team.manager, "pipeline:devteam", {**inputs, "task": epic.id}
    )
    outsider = world.child(leaf)
    with pytest.raises(TeamAdmissionRefused) as refused:
        await world.admit(outsider, leaf, parent_team_id=team.binding.team_id)
    assert refused.value.code == "pod_parent_invalid"
    single = new_task(world, "Single task")
    single_team = await epic_team(world, single)
    single_run = world.child(leaf, parent=single_team.manager)
    with pytest.raises(TeamAdmissionRefused) as refused:
        await world.admit(single_run, leaf, parent_team_id=single_team.binding.team_id)
    assert refused.value.code == "pod_parent_invalid"
    assert not dev_pod_launch_allowed(sessions, single_team.manager, "pipeline:devteam", inputs)

    # Exercise the bundled rule through real proxy normalization and caller identity.
    from gobby.hooks.events import HookEventType
    from gobby.workflows.engine.core import RuleEngine
    from tests.workflows.test_seat_rules import _event, _load_roles, _proxy_call

    _load_roles(team.db)
    engine = RuleEngine(team.db)
    for tool, arguments, allowed in (
        ("run_pipeline", {"name": "devteam", "inputs": inputs}, True),
        ("pipeline:devteam", inputs, True),
        ("run_pipeline", {"name": "other", "inputs": inputs}, False),
        ("pipeline:other", inputs, False),
        ("run_pipeline", {"name": "devteam", "inputs": {"task": leaf.id}}, False),
        ("pipeline:devteam", {**inputs, "task": epic.id}, False),
    ):
        event = _event(HookEventType.BEFORE_TOOL, _proxy_call("gobby-workflows", tool, arguments))
        event.metadata["_platform_session_id"] = team.manager
        response = await engine.evaluate(
            event, session_id=team.manager, variables={"_agent_type": "dev-manager"}
        )
        assert (response.decision != "block") is allowed, response


@pytest.mark.asyncio
async def test_pod_lands_and_closes_against_epic_branch(
    world: TeamWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.tasks.land_commit._refresh_index", AsyncMock())
    epic = new_task(world, "Epic integration", epic=True)
    leaf = new_task(world, "Pod implementation", parent=epic)
    team = await epic_team(world, epic)
    run = world.child(leaf, parent=team.manager)
    pod = await world.admit(run, leaf, parent_team_id=team.binding.team_id)
    leaf_team = EpicTeam(world, leaf, run, pod, team.manager)
    developer = leaf_team.seat("developer", "implementation")
    reviewer = leaf_team.seat("code-reviewer", "review")
    LocalTaskManager(team.db).claim_task(leaf.id, developer, force=True)
    sha = leaf_team.commit(leaf, "pod_feature.py")
    submit(team.db, developer, leaf, "implementation", "approved", 1, sha)
    submit(team.db, reviewer, leaf, "review", "approved", 1, sha)
    grant_step_writes(team.db, team.manager, world.case.project_id, "dev-manager", ("closing",))
    submit(team.db, team.manager, leaf, "closing", "approved", 1, sha)
    main_tip = git_in(world.case.repo, "rev-parse", "HEAD")
    target = await task_landing_target(team.db, leaf)
    assert target == (tree_of(team.binding), team.binding.branch)
    result = await land_candidate(
        team.db, task=leaf, caller_session_id=team.manager, commit_sha=sha
    )
    assert result.get("landed") is True, result
    assert git_in(tree_of(team.binding), "rev-parse", "HEAD") == sha
    assert git_in(world.case.repo, "rev-parse", "HEAD") == main_tip
    evaluation = await _evaluate_close(
        RegistryContext(task_manager=LocalTaskManager(team.db)),
        task_id=leaf.id,
        reason="done",
        commit_sha=sha,
        project_path=str(tree_of(pod)),
        closing_session_id=team.manager,
    )
    assert evaluation.checklist is not None, evaluation
    ancestry = next(
        gate for gate in evaluation.checklist.gates if gate.name == "integration_ancestry"
    )
    assert ancestry.status == "passed", ancestry
    assert ancestry.details["integration_ref"] == f"refs/heads/{team.binding.branch}"

    from gobby.agents.team_teardown import TEARDOWN_GRACE
    from gobby.mcp_proxy.tools import agent_cancellation
    from tests.fixtures.team_teardown_case import (
        Terminations,
        closed_at,
        daemon,
        session_status,
        teardown_pass,
    )

    primary = running_seat(world, team.runbook, "developer")
    writer = running_seat(world, run, "test-writer")
    runs = LocalAgentRunManager(team.db)
    assert writer.agent_run_id is not None
    runs.complete(writer.agent_run_id)
    resumed = runs.create(
        parent_session_id=team.manager,
        provider="claude",
        prompt="Resume the pod writer.",
        agent_name="test-writer",
        child_session_id=writer.id,
        worktree_id=pod.worktree_id,
        resume_metadata_json={"resume_previous_run_id": writer.agent_run_id},
    )
    runs.start(resumed.id)
    sessions = SessionManager(team.db)
    sessions.update_parent_session_id(writer.id, team.manager)
    sessions.update_terminal_pickup_metadata(writer.id, agent_run_id=resumed.id)
    rebound = sessions.rebind_resumed_terminal_session(
        writer.id,
        machine_id=require_machine_id(),
        project_id=world.case.project_id,
        source=writer.source,
        transcript_path=None,
        terminal_context=None,
        workflow_name=None,
        agent_depth=writer.agent_depth,
        sandbox_enabled=None,
    )
    assert rebound is not None and rebound.status == "active"
    terminations = Terminations(team.db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)
    with session_context_for_test(team.manager):
        closed_result = await create_task_registry(LocalTaskManager(team.db)).call(
            "close_task", {"task_id": leaf.id, "commit_sha": sha, "reason": "done"}
        )
    assert closed_result.get("closed") is True, closed_result
    closed = LocalTaskManager(team.db).get_task(leaf.id)
    await teardown_pass(team.db, daemon(team.db), closed_at(closed) + TEARDOWN_GRACE)
    assert set(terminations.ended()) == {developer, reviewer, writer.id}
    resumed_after = runs.get(resumed.id)
    assert resumed_after is not None and resumed_after.status == "cancelled"
    assert not tree_of(pod).exists()
    assert tree_of(team.binding).is_dir()
    assert session_status(team.db, primary.id) == "active"
    assert session_status(team.db, team.manager) == "active"
    new_task(world, "Unstarted next pod", parent=epic)
    team.link_tip()
    assert sync_epic_review(team.db, team.binding.team_id).epic_tip_ready


@pytest.mark.asyncio
async def test_hand_off_refuses_task_outside_epic_team(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    tools = create_task_registry(tasks)
    epic = new_task(world, TITLE, epic=True)
    outsider = new_task(world, "Outside the epic")
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    other_developer = team.seat("developer", "implementation")
    tasks.claim_task(outsider.id, developer, force=True)

    refused = await hand_off(tools, team, outsider, developer, other_developer)
    assert refused.get("error_code") == "team_manager_required", refused
    assert tasks.get_task(outsider.id).claimed_by_session_id == developer


@pytest.mark.asyncio
async def test_third_rejection_holds_only_its_leaf(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    epic = new_task(world, TITLE, epic=True)
    leaf, waiting = new_task(world, "Code leaf", parent=epic), new_task(world, "Next", parent=epic)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    tasks.claim_task(leaf.id, developer, force=True)
    reject_rounds(team, leaf, developer, reviewer, 3)
    assert tasks.get_task(leaf.id).escalated_at is not None

    # Exhausting a leaf's rounds never escalates its epic or stops unrelated pods.
    state = sync_epic_review(team.db, team.binding.team_id)
    assert (state.held, state.holds) == (False, (leaf.id,))
    assert tasks.get_task(epic.id).escalated_at is None
    assert not seat_team_held(team.db, team.binding)
    run = world.child(waiting, parent=team.manager)
    pod = await world.admit(run, waiting, parent_team_id=team.binding.team_id)
    assert pod.task_id == waiting.id

    # Triage's audited reset starts a new epoch and releases only the leaf's hold.
    epoch = tasks.get_task(leaf.id).review_epoch
    tasks.de_escalate_task(leaf.id, reason="Triage audited reset")
    assert tasks.get_task(leaf.id).review_epoch == epoch + 1
    lifted = sync_epic_review(team.db, team.binding.team_id)
    assert (lifted.held, lifted.holds) == (False, ())


@pytest.mark.asyncio
async def test_explicit_epic_hold_refuses_new_review_rounds(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    epic = new_task(world, TITLE, epic=True)
    waiting = new_task(world, "Next", parent=epic)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    tasks.escalate_task(epic.id, "Operator holds the entire epic")
    assert sync_epic_review(team.db, team.binding.team_id).held

    # In-flight implementation on another leaf still completes; its new review round does not.
    tasks.claim_task(waiting.id, developer, force=True)
    sha = team.commit(waiting, "next.py")
    submit(team.db, developer, waiting, "implementation", "approved", 1, sha)
    with pytest.raises(StepRecordError, match="held"):
        submit(team.db, reviewer, waiting, "review", "approved", 1, sha)
    assert not [r for r in list_task_step_records(team.db, waiting.id) if r.step == "review"]

    # The hold is read from the store, so a second attempt is refused the same way.
    assert sync_epic_review(team.db, team.binding.team_id).held
    with pytest.raises(StepRecordError, match="held"):
        submit(team.db, reviewer, waiting, "review", "approved", 1, sha)

    # Lifting the hold accepts the same round.
    tasks.de_escalate_task(epic.id, reason="Triage audited reset")
    assert not sync_epic_review(team.db, team.binding.team_id).held
    submit(team.db, reviewer, waiting, "review", "approved", 1, sha)
    statuses = [r.status for r in list_task_step_records(team.db, waiting.id) if r.step == "review"]
    assert statuses == ["approved"]


@pytest.mark.asyncio
async def test_blocked_leaf_allows_ready_sibling_tip(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    epic = new_task(world, TITLE, epic=True)
    leaf, ready = (
        new_task(world, "Blocked leaf", parent=epic),
        new_task(world, "Ready", parent=epic),
    )
    outside = new_task(world, "Outside blocker")
    TaskDependencyManager(world.case.db).add_dependency(leaf.id, outside.id, "blocks")
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    tasks.claim_task(ready.id, developer, force=True)
    sha = team.commit(ready, "ready.py")
    submit(team.db, developer, ready, "implementation", "approved", 1, sha)
    submit(team.db, reviewer, ready, "review", "approved", 1, sha)
    team.link_tip()

    # The outside blocker holds its dependent; the approved sibling tip remains ready.
    state = sync_epic_review(team.db, team.binding.team_id)
    assert not state.held
    assert set(state.holds) == {leaf.id, outside.id}
    assert state.epic_tip_ready
    current = tasks.get_task(epic.id)
    assert (current.escalated_at, current.claimed_by_session_id) == (None, None)

    # Nothing is detached or reverted.
    held_leaf = tasks.get_task(leaf.id)
    assert (held_leaf.parent_task_id, held_leaf.closed_at) == (epic.id, None)
    assert tasks.get_task(ready.id).commits == [sha]
    assert git_in(tree_of(team.binding), "rev-parse", "HEAD") == sha

    tasks.close_task(outside.id, reason="done")
    assert not sync_epic_review(team.db, team.binding.team_id).held


@pytest.mark.asyncio
async def test_epic_tip_rejection_repairs_through_leaf(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    epic = new_task(world, TITLE, epic=True)
    first, second = new_task(world, "First", parent=epic), new_task(world, "Second", parent=epic)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    tip_reviewer = team.seat("code-reviewer", "review")
    for leaf in (first, second):
        tasks.claim_task(leaf.id, developer, force=True)
        sha = team.commit(leaf, f"leaf_{leaf.seq_num}.py")
        submit(team.db, developer, leaf, "implementation", "approved", 1, sha)
        submit(team.db, reviewer, leaf, "review", "approved", 1, sha)
    tip = team.link_tip()
    assert sync_epic_review(team.db, team.binding.team_id).epic_tip_ready

    # The epic tip is rejected once: no escalation, and no new round until a leaf is repaired.
    submit(team.db, tip_reviewer, epic, "review", "rejected", 1, tip)
    assert tasks.get_task(epic.id).escalated_at is None
    assert not sync_epic_review(team.db, team.binding.team_id).epic_tip_ready

    # The finding goes to the leaf that owns it; only its records go stale.
    tasks.claim_task(first.id, developer, force=True)
    fix = team.commit(first, "first_fix.py")
    submit(team.db, developer, first, "implementation", "approved", 2, fix)
    assert not approved(team.db, first, "review")
    assert approved(team.db, second, "review")
    assert not sync_epic_review(team.db, team.binding.team_id).epic_tip_ready
    # The leaf has no rejection of its own, so its rewritten review is its round one.
    submit(team.db, reviewer, first, "review", "approved", 1, fix)
    new_tip = team.link_tip()
    assert sync_epic_review(team.db, team.binding.team_id).epic_tip_ready

    # The second epic-tip round approves the new tip, with the first rejection still counted.
    submit(team.db, tip_reviewer, epic, "review", "approved", 2, new_tip)
    landed = tasks.get_task(epic.id)
    assert approved(team.db, epic, "review")
    assert (landed.review_fail_count, landed.escalated_at) == (1, None)
