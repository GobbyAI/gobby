"""A whole epic team from admission to teardown, and its two recoveries (addendum 8.6).

The Dev Manager of each epic holds no claim (Decision 16). Developers, test writers and
reviewers are sessions bound to the team's runbook, each granted the one step its
definition writes. The team's own gates decide every landing, hold and close.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest

from gobby.agents.agent_workspaces import (
    bind_epic_workspace,
    drain_workspace_commits,
    workspace_git_dir,
)
from gobby.agents.team_lifecycle import TeamBinding
from gobby.agents.team_review import sync_epic_review
from gobby.agents.team_teardown import TEARDOWN_GRACE, TEARDOWN_KEY
from gobby.mcp_proxy.tools import agent_cancellation
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.tasks.step_records import (
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
    running_seat,
    synthetic_manager_body,
)
from tests.fixtures.team_teardown_case import (
    Terminations,
    branch_exists,
    closed_at,
    daemon,
    session_status,
    teardown_pass,
)

pytestmark = pytest.mark.e2e

_REAL_AGENT_BODY = _factory._load_agent_body
LEAF_TITLE = "Wire the scroll handler"
# Definitions by the one step each writes; the Dev Manager is the shipped definition.
DEFINITIONS: dict[str, tuple[str, ...]] = {
    "developer": ("implementation",),
    "test-writer": ("test",),
    "code-reviewer": ("review",),
    "docs-reviewer": ("review",),
    "research-reviewer": ("review",),
}
Disposition = Literal["moot", "superseded", "wont_do"]
# The project enforces step records for its tasks, as the devteam pilot does (plan 8.6).
CLOSE_POLICY = {
    "enforce_step_records": True,
    "test_categories": ["code"],
    "review_categories": ["code", "refactor", "docs", "research"],
}


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

    @property
    def tree(self) -> Path:
        assert self.binding.worktree_path is not None
        return Path(self.binding.worktree_path)

    def seat(self, definition: str, step: TaskStep) -> str:
        """A running seat the runbook launched as ``definition``, granted only ``step``."""
        session = running_seat(self.world, self.runbook, definition)
        grant_step_writes(self.db, session.id, self.world.case.project_id, definition, (step,))
        return session.id

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
    monkeypatch.setenv("GOBBY_HOME", str((tmp_path / "gobby-home").resolve()))
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
        definitions.create(
            name,
            {
                "name": name,
                "version": "1.0",
                "prompts": {"agent": f"Run the {name} seat."},
                "workflows": {"rule_selectors": {"include": []}},
                "task_step_writes": list(steps),
            },
            project_id=project_id,
        )
    definitions.create("dev-manager", synthetic_manager_body(), project_id=project_id)
    return world


def adopt_close_policy(world: TeamWorld) -> None:
    """Commit the project's close policy before any team admits, so its branch starts from it."""
    project_json = {"id": world.case.project_id, "task_close_policy": CLOSE_POLICY}
    world.case.direct({".gobby/project.json": json.dumps(project_json) + "\n"})


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
    """An admitted epic team with its running Dev Manager seat; the manager never claims."""
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


def commit_work(db: HubDatabase, tree: Path, task: Task, name: str) -> str:
    """Commit ``name`` in ``tree`` and link it to ``task``, as a seat does after its work."""
    path = tree / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {name}\n")
    git_in(tree, "add", name)
    git_in(tree, "commit", "-m", f"[gobby-#{task.seq_num}] feat: {name}")
    sha = git_in(tree, "rev-parse", "HEAD")
    LocalTaskManager(db).link_commit(task.id, sha)
    return sha


def submit(
    db: HubDatabase,
    session_id: str,
    task: Task,
    step: TaskStep,
    status: StepStatus,
    round_number: int,
    *,
    sha: str | None = None,
    disposition: Disposition | None = None,
) -> None:
    """Record ``session_id``'s result for ``step`` at the task's current review epoch."""
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
            disposition=disposition,
        ),
    )


def approved(db: HubDatabase, task: Task, step: TaskStep) -> bool:
    current = LocalTaskManager(db).get_task(task.id)
    return approved_task_step(current, list_task_step_records(db, task.id), step) is not None


def ready_ids(world: TeamWorld) -> set[str]:
    """The task ids the ready queue offers now, with team-resolved blockers applied."""
    tasks = LocalTaskManager(world.case.db)
    ready = tasks.list_ready_tasks(project_id=world.case.project_id, limit=500)
    return {task.id for task in ready}


async def hand_off(
    world: TeamWorld, team: EpicTeam, leaf: Task, holder: str, recipient: str
) -> None:
    """The Dev Manager moves a leaf's claim between its seats with hand_off_task_claim."""
    tasks = LocalTaskManager(world.case.db)
    with session_context_for_test(team.manager):
        reply = await create_task_registry(tasks).call(
            "hand_off_task_claim",
            {
                "task_id": leaf.id,
                "holder_session_ref": holder,
                "recipient_session_ref": recipient,
                "reason": "Hand the leaf to the seat that works it",
            },
        )
    assert reply.get("success") is True, reply


def sub_worktree(team: EpicTeam, leaf: Task) -> tuple[str, Path, str]:
    """A parallel leaf's sub-worktree off epic main, created and registered as dispatch does."""
    branch = f"epic/{team.epic.seq_num}/{leaf.seq_num}"
    path = team.tree.parent / f"sub-{team.epic.seq_num}-{leaf.seq_num}"
    git_in(team.tree, "worktree", "add", "-b", branch, str(path), team.binding.branch)
    row = LocalWorktreeManager(team.db).create(
        team.world.case.project_id,
        branch_name=branch,
        worktree_path=str(path),
        base_branch=team.binding.branch,
        task_id=leaf.id,
    )
    return row.id, path, branch


def teardown_outcome(world: TeamWorld, runbook: Session) -> dict[str, Any]:
    """The teardown outcome the pass recorded on the team's admission output."""
    record = world.admit_step_output(runbook).get(TEARDOWN_KEY)
    return record if isinstance(record, dict) else {}


def epic_dir(world: TeamWorld, epic: Task) -> Path:
    home = Path(os.environ["GOBBY_HOME"])
    return home / "agents_workspaces" / world.case.project_id / "epics" / f"epic-{epic.seq_num}"


def workspace_subjects(project_id: str) -> list[str]:
    result = subprocess.run(
        ["git", f"--git-dir={workspace_git_dir(project_id)}", "log", "--format=%s"],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.splitlines() if result.returncode == 0 else []


@pytest.mark.asyncio
async def test_complete_epic_team(world: TeamWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """Six leaves use real pods, test review, per-leaf landing and scoped teardown (8.6.1)."""
    adopt_close_policy(world)
    db = world.case.db
    tasks = LocalTaskManager(db)
    terminations = Terminations(db)
    monkeypatch.setattr(agent_cancellation, "terminate_agent_run", terminations)

    epic = new_task(world, TITLE, epic=True)
    phase = new_task(world, "Scroll state phase", parent=epic, epic=True)
    backend = new_task(world, "Keep scroll state in the store", parent=phase)
    frontend = new_task(world, "Restore scroll position in the view", parent=phase)
    wire = new_task(world, LEAF_TITLE, parent=phase)
    retire = new_task(world, "Retire the legacy scroll flag", parent=epic)
    document = new_task(world, "Document the scroll flag", parent=epic)
    survey = new_task(world, "Survey scroll libraries", parent=epic)
    for leaf, category in (
        (backend, "code"),
        (frontend, "code"),
        (wire, "code"),
        (retire, "refactor"),
        (document, "docs"),
        (survey, "research"),
    ):
        tasks.update_task(leaf.id, category=category)
    tasks.update_task(backend.id, implementation_domain="backend")
    tasks.update_task(frontend.id, implementation_domain="frontend")
    deps = TaskDependencyManager(db)
    deps.add_dependency(wire.id, backend.id, "blocks")
    deps.add_dependency(wire.id, frontend.id, "blocks")
    deps.add_dependency(document.id, retire.id, "blocks")

    team = await epic_team(world, epic)
    tree, manager = team.tree, team.manager
    writer = team.seat("test-writer", "test")
    primary = team.seat("developer", "implementation")
    code_reviewer = team.seat("code-reviewer", "review")
    primary_seats = {manager, writer, primary, code_reviewer}
    folder = epic_dir(world, epic)
    assert bind_epic_workspace(db, manager) == folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "decisions.md").write_text("Two Dev Pods feed one primary leaf.\n")
    integration_tip = world.case.git("rev-parse", "trunk")

    # Admit both pods before either works, so their branches genuinely diverge.
    pods: list[EpicTeam] = []
    for leaf in (backend, frontend):
        runbook = world.child(leaf, parent=manager)
        binding = await world.admit(runbook, leaf, parent_team_id=team.binding.team_id)
        pods.append(EpicTeam(world, leaf, runbook, binding, manager))
    assert pods[0].tree != pods[1].tree != tree

    async def deliver(
        scope: EpicTeam,
        leaf: Task,
        maker: str,
        reviewer: str,
        test_writer: str | None,
        *,
        fix_rounds: bool = False,
    ) -> tuple[str, set[str]]:
        seats = {maker, reviewer}
        if test_writer is not None:
            seats.add(test_writer)
            tasks.claim_task(leaf.id, test_writer, force=True)
            tests_sha = commit_work(db, scope.tree, leaf, f"tests/test_{leaf.seq_num}.py")
            submit(db, test_writer, leaf, "test", "approved", 1, sha=tests_sha)
            submit(db, reviewer, leaf, "review", "approved", 1, sha=tests_sha)
            assert approved(db, leaf, "review")
            await hand_off(world, team, leaf, test_writer, maker)
        else:
            tasks.claim_task(leaf.id, maker, force=True)
        sha = commit_work(db, scope.tree, leaf, f"leaf_{leaf.seq_num}.py")
        submit(db, maker, leaf, "implementation", "approved", 1, sha=sha)
        if fix_rounds:
            for round_number in (1, 2):
                submit(db, reviewer, leaf, "review", "rejected", round_number, sha=sha)
                if round_number == 1:
                    assert tasks.get_task(leaf.id).claimed_by_session_id == maker
                else:
                    await hand_off(world, team, leaf, maker, reviewer)
                    maker = reviewer
                    fresh_runbook = world.child(leaf, parent=manager)
                    fresh_binding = await world.admit(
                        fresh_runbook,
                        leaf,
                        scope.binding.team_id,
                        fresh_reviewer=True,
                        exclude_reviewers=[reviewer],
                    )
                    fresh = EpicTeam(world, leaf, fresh_runbook, fresh_binding, manager)
                    reviewer = fresh.seat("code-reviewer", "review")
                    seats.add(reviewer)
                sha = commit_work(db, scope.tree, leaf, f"fix_{leaf.seq_num}_{round_number}.py")
                submit(db, maker, leaf, "implementation", "approved", round_number + 1, sha=sha)
        submit(db, reviewer, leaf, "review", "approved", 3 if fix_rounds else 1, sha=sha)
        assert approved(db, leaf, "review")
        submit(db, manager, leaf, "closing", "approved", 1, sha=sha)
        landed = await world.case.land(tasks.get_task(leaf.id), sha, caller=manager)
        assert landed.get("landed") is True, landed
        assert world.case.git("rev-parse", "trunk") == integration_tip
        with session_context_for_test(manager):
            closed = await create_task_registry(tasks).call(
                "close_task", {"task_id": leaf.id, "commit_sha": sha, "reason": "done"}
            )
        assert closed.get("closed") is True, closed
        assert tasks.get_task(leaf.id).closed_at is not None
        return str(landed["mode"]), seats

    modes = set()
    for index, pod in enumerate(pods):
        pod_writer = pod.seat("test-writer", "test")
        pod_maker = pod.seat("developer", "implementation")
        pod_reviewer = pod.seat("code-reviewer", "review")
        for seat in (pod_writer, pod_maker, pod_reviewer):
            assert bind_epic_workspace(db, seat) == folder
        assert wire.id not in ready_ids(world)
        mode, seats = await deliver(
            pod, pod.epic, pod_maker, pod_reviewer, pod_writer, fix_rounds=index == 1
        )
        modes.add(mode)
        assert (
            await teardown_pass(
                db, daemon(db), closed_at(tasks.get_task(pod.epic.id)) + TEARDOWN_GRACE
            )
            > 0
        )
        assert seats <= set(terminations.ended())
        assert not primary_seats & set(terminations.ended())
        assert not pod.tree.exists()
        assert not branch_exists(world, pod.binding.branch)
        assert all(session_status(db, seat) == "active" for seat in primary_seats)
    assert modes == {"ff", "merge"}
    assert wire.id in ready_ids(world)

    await deliver(team, wire, primary, code_reviewer, writer)
    assert tasks.get_task(phase.id).closed_at is not None
    assert tasks.get_task(epic.id).closed_at is None
    assert (
        await teardown_pass(db, daemon(db), closed_at(tasks.get_task(wire.id)) + TEARDOWN_GRACE)
        == 0
    )

    # No-work is reviewed, then the operator closes it; the dependent docs leaf unlocks.
    assert document.id not in ready_ids(world)
    submit(db, code_reviewer, retire, "review", "approved", 1, disposition="moot")
    assert {record.step for record in list_task_step_records(db, retire.id)} == {"review"}
    with session_context_for_test(world.case.creator):
        closed = await create_task_registry(tasks).call(
            "close_task", {"task_id": retire.id, "reason": "moot"}
        )
    assert closed.get("closed") is True, closed
    assert document.id in ready_ids(world)
    docs_reviewer = team.seat("docs-reviewer", "review")
    research_reviewer = team.seat("research-reviewer", "review")
    await deliver(team, document, primary, docs_reviewer, None)
    await deliver(team, survey, primary, research_reviewer, None)
    assert all(
        tasks.get_task(leaf.id).closed_at is not None
        for leaf in (backend, frontend, wire, retire, document, survey)
    )
    assert not primary_seats & set(terminations.ended())

    # Final integration and exact-tip review happen after each leaf has already closed.
    world.case.direct({"release/0.5.0.txt": "Released while the epic ran.\n"})
    git_in(tree, "merge", "--no-ff", "trunk", "-m", "Merge 0.5.0 into epic main")
    tip = git_in(tree, "rev-parse", "HEAD")
    tasks.link_commit(epic.id, tip)
    assert sync_epic_review(db, team.binding.team_id).epic_tip_ready
    submit(db, code_reviewer, epic, "review", "approved", 1, sha=tip)
    refused = await world.case.land(tasks.get_task(epic.id), tip, caller=code_reviewer)
    assert refused["landed"] is False, refused
    assert "caller_not_task_manager" in refused["blockers"], refused
    submit(db, manager, epic, "closing", "approved", 1, sha=tip)
    landed = await world.case.land(tasks.get_task(epic.id), tip, caller=manager)
    assert (landed["landed"], landed["worktree_merged"]) == (True, True), landed
    assert world.case.git("rev-parse", "trunk") == tip
    with session_context_for_test(manager):
        closed = await create_task_registry(tasks).call(
            "close_task", {"task_id": epic.id, "commit_sha": tip, "reason": "done"}
        )
    assert closed.get("closed") is True, closed

    epic_closed = closed_at(tasks.get_task(epic.id))
    runner = daemon(db)
    assert await teardown_pass(db, runner, epic_closed) == 0
    assert await teardown_pass(db, runner, epic_closed + TEARDOWN_GRACE) > 0
    assert await teardown_pass(db, runner, epic_closed + TEARDOWN_GRACE) == 0
    assert teardown_outcome(world, team.runbook) == {"outcome": "cleaned"}
    assert primary_seats <= set(terminations.ended())
    assert session_status(db, manager) == "expired"
    assert not tree.exists()
    assert team.binding.worktree_id is not None
    assert LocalWorktreeManager(db).get(team.binding.worktree_id) is None
    assert not branch_exists(world, team.binding.branch)
    await drain_workspace_commits()
    assert any(
        f"epic-{epic.seq_num}" in subject for subject in workspace_subjects(world.case.project_id)
    )


@pytest.mark.asyncio
async def test_ready_tip_lands_while_sibling_is_escalated(world: TeamWorld) -> None:
    """Ready siblings land before an isolated rejected leaf is reset and merged."""
    adopt_close_policy(world)
    db = world.case.db
    tasks = LocalTaskManager(db)
    epic = new_task(world, TITLE, epic=True)
    stuck = new_task(world, "Third-rejected leaf", parent=epic)
    done = new_task(world, "Finished leaf", parent=epic)
    waiting = new_task(world, LEAF_TITLE, parent=epic)
    team = await epic_team(world, epic)
    tree, manager = team.tree, team.manager
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    _, stuck_tree, stuck_branch = sub_worktree(team, stuck)

    # Three implementation rounds, each rejected: the third rejection escalates the leaf.
    tasks.claim_task(stuck.id, developer, force=True)
    for number in (1, 2, 3):
        sha = commit_work(db, stuck_tree, stuck, f"stuck_round_{number}.py")
        submit(db, developer, stuck, "implementation", "approved", number, sha=sha)
        submit(db, reviewer, stuck, "review", "rejected", number, sha=sha)
    assert tasks.get_task(stuck.id).escalated_at is not None

    tasks.claim_task(done.id, developer, force=True)
    done_sha = commit_work(db, tree, done, "done.py")
    submit(db, developer, done, "implementation", "approved", 1, sha=done_sha)
    submit(db, reviewer, done, "review", "approved", 1, sha=done_sha)

    # The rejected branch and unstarted sibling never hold the ready epic tip.
    state = sync_epic_review(db, team.binding.team_id)
    assert (state.held, state.holds) == (False, (stuck.id,))
    assert tasks.get_task(epic.id).escalated_at is None

    # The tip still requires its own review, then lands while the sibling stays escalated.
    tip = git_in(tree, "rev-parse", "HEAD")
    tasks.link_commit(epic.id, tip)
    assert sync_epic_review(db, team.binding.team_id).epic_tip_ready
    unreviewed = await world.case.land(tasks.get_task(epic.id), tip, caller=manager)
    assert unreviewed["landed"] is False, unreviewed
    assert "review_approval_missing" in unreviewed["blockers"], unreviewed
    tip_reviewer = team.seat("code-reviewer", "review")
    submit(db, tip_reviewer, epic, "review", "approved", 1, sha=tip)
    landed = await world.case.land(tasks.get_task(epic.id), tip, caller=manager)
    assert (landed["landed"], landed["worktree_merged"]) == (True, True), landed
    assert world.case.git("rev-parse", "trunk") == tip
    assert tasks.get_task(stuck.id).escalated_at is not None
    assert not tasks.get_task(waiting.id).commits

    # Triage's audited reset starts a new review epoch for only the rejected leaf.
    epoch = tasks.get_task(stuck.id).review_epoch
    tasks.de_escalate_task(stuck.id, reason="Triage audited reset")
    assert tasks.get_task(stuck.id).review_epoch == epoch + 1
    lifted = sync_epic_review(db, team.binding.team_id)
    assert (lifted.held, lifted.holds) == (False, ())

    # The reset leaf is worked again in its new epoch, and the waiting leaf finishes.
    tasks.claim_task(stuck.id, developer, force=True)
    fix = commit_work(db, stuck_tree, stuck, "stuck_fix.py")
    submit(db, developer, stuck, "implementation", "approved", 1, sha=fix)
    submit(db, reviewer, stuck, "review", "approved", 1, sha=fix)
    git_in(tree, "merge", "--no-ff", stuck_branch, "-m", "Merge repaired leaf")
    waiting_dev = team.seat("developer", "implementation")
    tasks.claim_task(waiting.id, waiting_dev, force=True)
    waiting_sha = commit_work(db, tree, waiting, "waiting.py")
    submit(db, waiting_dev, waiting, "implementation", "approved", 1, sha=waiting_sha)
    submit(db, reviewer, waiting, "review", "approved", 1, sha=waiting_sha)

    # The epic tip is approved by the epic-tip reviewer and lands through the Dev Manager.
    tip = git_in(tree, "rev-parse", "HEAD")
    tasks.link_commit(epic.id, tip)
    assert sync_epic_review(db, team.binding.team_id).epic_tip_ready
    submit(db, tip_reviewer, epic, "review", "approved", 1, sha=tip)
    landed = await world.case.land(tasks.get_task(epic.id), tip, caller=manager)
    assert (landed["landed"], landed["worktree_merged"]) == (True, True), landed
    assert world.case.git("rev-parse", "trunk") == tip
    assert tasks.get_task(epic.id).escalated_at is None


@pytest.mark.asyncio
async def test_epic_tip_rejection_repaired_then_lands(world: TeamWorld) -> None:
    """An epic-tip rejection is repaired through its owning leaf and lands on round two (8.6.3)."""
    adopt_close_policy(world)
    db = world.case.db
    tasks = LocalTaskManager(db)
    epic = new_task(world, TITLE, epic=True)
    first = new_task(world, "First leaf", parent=epic)
    second = new_task(world, "Second leaf", parent=epic)
    team = await epic_team(world, epic)
    tree, manager = team.tree, team.manager
    developer = team.seat("developer", "implementation")
    reviewer = team.seat("code-reviewer", "review")
    tip_reviewer = team.seat("code-reviewer", "review")

    # Every leaf is implemented and approved before the epic tip is reviewed.
    for leaf in (first, second):
        tasks.claim_task(leaf.id, developer, force=True)
        sha = commit_work(db, tree, leaf, f"leaf_{leaf.seq_num}.py")
        submit(db, developer, leaf, "implementation", "approved", 1, sha=sha)
        submit(db, reviewer, leaf, "review", "approved", 1, sha=sha)
    tip = git_in(tree, "rev-parse", "HEAD")
    tasks.link_commit(epic.id, tip)
    assert sync_epic_review(db, team.binding.team_id).epic_tip_ready

    # The first epic-tip round rejects the tip: no escalation, and no new round until repaired.
    submit(db, tip_reviewer, epic, "review", "rejected", 1, sha=tip)
    assert tasks.get_task(epic.id).escalated_at is None
    assert not sync_epic_review(db, team.binding.team_id).epic_tip_ready

    # The finding concerns the first leaf alone; the second leaf keeps its approvals.
    fix = commit_work(db, tree, first, "first_fix.py")
    submit(db, developer, first, "implementation", "approved", 2, sha=fix)
    assert not approved(db, first, "review")
    assert approved(db, second, "review")
    assert not sync_epic_review(db, team.binding.team_id).epic_tip_ready
    # The leaf has no rejected review of its own, so its re-review is round one of its epoch.
    submit(db, reviewer, first, "review", "approved", 1, sha=fix)
    new_tip = git_in(tree, "rev-parse", "HEAD")
    tasks.link_commit(epic.id, new_tip)
    assert sync_epic_review(db, team.binding.team_id).epic_tip_ready

    # The second epic-tip round approves the new tip; the first rejection stays counted.
    submit(db, tip_reviewer, epic, "review", "approved", 2, sha=new_tip)
    current = tasks.get_task(epic.id)
    assert approved(db, epic, "review")
    assert (current.review_fail_count, current.escalated_at) == (1, None)

    landed = await world.case.land(current, new_tip, caller=manager)
    assert (landed["landed"], landed["worktree_merged"]) == (True, True), landed
    assert world.case.git("rev-parse", "trunk") == new_tip
