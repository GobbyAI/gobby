"""Epic-mode review: no reviewer fixer, epic holds and epic-tip repair through leaves (addendum 8.4)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.agents.team_lifecycle import TeamAdmissionRefused, TeamBinding
from gobby.agents.team_review import sync_epic_review
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.step_records import (
    StepRecordError,
    StepStatus,
    StepSubmission,
    TaskStep,
    approved_task_step,
    list_task_step_records,
    record_task_step,
)
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.review_approvals import grant_step_writes
from tests.fixtures.team_case import (
    TITLE,
    TeamWorld,
    git_in,
    make_team_world,
    synthetic_manager_body,
)

pytestmark = pytest.mark.unit

# make_team_world stubs the definition loader; the held-spawn checks need installed definitions.
_REAL_AGENT_BODY = _factory._load_agent_body


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
        tree = Path(self.binding.worktree_path)
        (tree / name).write_text(f"# {name}\n")
        git_in(tree, "add", name)
        git_in(tree, "commit", "-m", f"[gobby-#{task.seq_num}] feat: {name}")
        sha = git_in(tree, "rev-parse", "HEAD")
        link(self.db, task, sha)
        return sha

    def link_tip(self) -> str:
        """Link the epic tree's tip to the epic, as the Dev Manager does before the epic review."""
        sha = git_in(self.binding.worktree_path, "rev-parse", "HEAD")
        link(self.db, self.epic, sha)
        return sha

    async def spawn_developer(self, leaf: Task) -> dict[str, Any]:
        return await self.world.direct_spawn(
            self.manager, agent="developer", task_id=leaf.id, checkout_mode="worktree"
        )


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
async def test_reviewer_never_fixes_in_epic_mode(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    tools = create_task_registry(tasks)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, "Code leaf", parent=epic)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    tasks.claim_task(leaf.id, developer, force=True)
    sha = reject_rounds(team, leaf, developer, reviewer, 2)

    # Each rejection returns the leaf to a developer; the reviewer never takes the claim.
    other_developer = team.seat("developer", "implementation")
    moved = await hand_off(tools, team, leaf, developer, other_developer)
    assert moved.get("success") is True, moved
    refused = await hand_off(tools, team, leaf, other_developer, reviewer)
    assert refused.get("error_code") == "reviewer_claim_forbidden", refused
    assert tasks.get_task(leaf.id).claimed_by_session_id == other_developer
    with pytest.raises(StepRecordError, match="cannot write the implementation step"):
        submit(team.db, reviewer, leaf, "implementation", "approved", 3, sha)

    # No fresh reviewer joins an epic team.
    fresh = world.child(epic)
    with pytest.raises(TeamAdmissionRefused) as refusal:
        await world.admit(
            fresh, epic, team.binding.team_id, fresh_reviewer=True, exclude_reviewers=[reviewer]
        )
    assert refusal.value.code == "fresh_reviewer_epic_mode"

    # The definition that wrote the leaf's earlier reviews reviews its next round.
    state = sync_epic_review(team.db, team.binding.team_id)
    assert state.next_reviewers == {leaf.id: "code-reviewer"}
    assert (state.held, state.holds) == (False, ())


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
async def test_third_rejection_holds_epic(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    epic = new_task(world, TITLE, epic=True)
    leaf, waiting = new_task(world, "Code leaf", parent=epic), new_task(world, "Next", parent=epic)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    tasks.claim_task(leaf.id, developer, force=True)
    reject_rounds(team, leaf, developer, reviewer, 3)
    assert tasks.get_task(leaf.id).escalated_at is not None

    # The team escalates its epic once, through the claim-releasing escalation.
    state = sync_epic_review(team.db, team.binding.team_id)
    assert state.held and leaf.id in state.holds
    held_epic = tasks.get_task(epic.id)
    assert held_epic.escalated_at is not None
    assert held_epic.claimed_by_session_id is None
    assert leaf.id in (held_epic.escalation_reason or "")
    again = sync_epic_review(team.db, team.binding.team_id)
    assert again.held
    assert tasks.get_task(epic.id).escalated_at == held_epic.escalated_at

    # While held, the manager dispatches nothing.
    before = len(world.launches)
    refused = await team.spawn_developer(waiting)
    assert refused.get("success") is not True, refused
    assert len(world.launches) == before

    # Triage's audited reset starts a new epoch; de-escalation and re-claim lift the hold.
    epoch = tasks.get_task(leaf.id).review_epoch
    tasks.de_escalate_task(leaf.id, reason="Triage audited reset")
    assert tasks.get_task(leaf.id).review_epoch == epoch + 1
    tasks.de_escalate_task(epic.id, reason="Triage audited reset")
    lifted = sync_epic_review(team.db, team.binding.team_id)
    assert (lifted.held, lifted.holds) == (False, ())
    allowed = await team.spawn_developer(waiting)
    assert allowed.get("success") is True, allowed


@pytest.mark.asyncio
async def test_held_team_refuses_a_new_review_round_on_another_leaf(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    epic = new_task(world, TITLE, epic=True)
    leaf, waiting = new_task(world, "Code leaf", parent=epic), new_task(world, "Next", parent=epic)
    team = await epic_team(world, epic)
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    tasks.claim_task(leaf.id, developer, force=True)
    reject_rounds(team, leaf, developer, reviewer, 3)
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
    tasks.de_escalate_task(leaf.id, reason="Triage audited reset")
    tasks.de_escalate_task(epic.id, reason="Triage audited reset")
    assert not sync_epic_review(team.db, team.binding.team_id).held
    submit(team.db, reviewer, waiting, "review", "approved", 1, sha)
    statuses = [r.status for r in list_task_step_records(team.db, waiting.id) if r.step == "review"]
    assert statuses == ["approved"]


@pytest.mark.asyncio
async def test_blocked_leaf_holds_epic(world: TeamWorld) -> None:
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
    tasks.claim_task(ready.id, developer, force=True)
    sha = team.commit(ready, "ready.py")
    submit(team.db, developer, ready, "implementation", "approved", 1, sha)

    # The outside blocker holds the epic without escalating it.
    state = sync_epic_review(team.db, team.binding.team_id)
    assert (state.held, state.holds) == (True, (leaf.id,))
    assert not state.epic_tip_ready
    current = tasks.get_task(epic.id)
    assert (current.escalated_at, current.claimed_by_session_id) == (None, None)

    # Nothing is detached or reverted.
    held_leaf = tasks.get_task(leaf.id)
    assert (held_leaf.parent_task_id, held_leaf.closed_at) == (epic.id, None)
    assert tasks.get_task(ready.id).commits == [sha]
    assert git_in(team.binding.worktree_path, "rev-parse", "HEAD") == sha

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
