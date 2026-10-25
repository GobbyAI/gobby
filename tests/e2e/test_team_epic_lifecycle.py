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
from functools import partial
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml

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
from gobby.worktrees.deletion import (
    DeletionSurface,
    WorktreeDeletionRequest,
    WorktreeDeletionResult,
    delete_worktree_transaction,
)
from gobby.worktrees.executor import run_worktree_delete
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.review_approvals import grant_step_writes
from tests.fixtures.team_case import TITLE, TeamWorld, git_in, make_team_world
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
DEV_MANAGER_YAML = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/workflows/agents/dev-manager.yaml"
)
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
        return Path(self.binding.worktree_path)

    def seat(self, definition: str, step: TaskStep) -> str:
        """A running seat the runbook launched as ``definition``, granted only ``step``."""
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
    manager_body = yaml.safe_load(DEV_MANAGER_YAML.read_text())
    definitions.create("dev-manager", manager_body, project_id=project_id)
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
    """The six-leaf nested epic completes from admission to exactly one teardown (8.6.1)."""
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
    deps = TaskDependencyManager(db)
    deps.add_dependency(wire.id, backend.id, "blocks")
    deps.add_dependency(wire.id, frontend.id, "blocks")
    deps.add_dependency(document.id, retire.id, "blocks")

    team = await epic_team(world, epic)
    tree, manager = team.tree, team.manager
    writer = team.seat("test-writer", "test")
    backend_dev = team.seat("developer", "implementation")
    frontend_dev = team.seat("developer", "implementation")
    primary = team.seat("developer", "implementation")
    code_reviewer = team.seat("code-reviewer", "review")
    docs_reviewer = team.seat("docs-reviewer", "review")
    research_reviewer = team.seat("research-reviewer", "review")

    # The epic's workspace folder is bound before any seat writes its notes.
    folder = epic_dir(world, epic)
    assert bind_epic_workspace(db, manager) == folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "decisions.md").write_text("Two sub-worktrees feed one primary leaf.\n")

    # Test phase: the Test Writer commits each code leaf's tests, then hands the claim over.
    for leaf in (backend, frontend, wire):
        tasks.claim_task(leaf.id, writer, force=True)
        tests_sha = commit_work(db, tree, leaf, f"tests/test_{leaf.seq_num}.py")
        submit(db, writer, leaf, "test", "approved", 1, sha=tests_sha)
    await hand_off(world, team, backend, writer, backend_dev)
    await hand_off(world, team, frontend, writer, frontend_dev)

    # Test commits are registered commits, but they finish nothing: the dependent stays blocked.
    assert wire.id not in ready_ids(world)

    # Two parallel code leaves each get a sub-worktree; each merges back with a no-ff merge.
    backend_row, backend_path, backend_branch = sub_worktree(team, backend)
    backend_sha = commit_work(db, backend_path, backend, "backend.py")
    submit(db, backend_dev, backend, "implementation", "approved", 1, sha=backend_sha)
    git_in(tree, "merge", "--no-ff", backend_branch, "-m", "Merge backend leaf into epic main")
    merged = await delete_tree(world, backend_row, team.binding.branch)
    assert merged.success, merged
    assert not backend_path.exists()
    # One finished blocker does not unlock a dependent that still waits on its parallel pair.
    assert wire.id not in ready_ids(world)

    frontend_row, frontend_path, frontend_branch = sub_worktree(team, frontend)
    frontend_sha = commit_work(db, frontend_path, frontend, "frontend.py")
    submit(db, frontend_dev, frontend, "implementation", "approved", 1, sha=frontend_sha)
    git_in(tree, "merge", "--no-ff", frontend_branch, "-m", "Merge frontend leaf into epic main")
    merged = await delete_tree(world, frontend_row, team.binding.branch)
    assert merged.success, merged
    assert not frontend_path.exists()
    # Both parallel blockers finished, so the dependent is ready and goes to the primary.
    assert wire.id in ready_ids(world)
    await hand_off(world, team, wire, writer, primary)

    # The primary works the dependent leaf in the epic worktree itself.
    wire_sha = commit_work(db, tree, wire, "wire.py")
    submit(db, primary, wire, "implementation", "approved", 1, sha=wire_sha)

    # The no-work leaf records its disposition; the docs leaf it blocks unlocks with it.
    assert document.id not in ready_ids(world)
    submit(db, primary, retire, "implementation", "approved", 1, disposition="moot")
    assert document.id in ready_ids(world)
    tasks.claim_task(document.id, primary, force=True)
    doc_sha = commit_work(db, tree, document, "docs/scroll-flag.md")
    submit(db, primary, document, "implementation", "approved", 1, sha=doc_sha)
    tasks.claim_task(survey.id, primary, force=True)
    survey_sha = commit_work(db, tree, survey, "research/scroll-libraries.md")
    submit(db, primary, survey, "implementation", "approved", 1, sha=survey_sha)

    # Per-leaf review: each leaf goes to its routed reviewer; one code rejection returns to a
    # developer, who fixes the leaf in the epic worktree and is re-reviewed by the same seat.
    submit(db, code_reviewer, backend, "review", "approved", 1, sha=backend_sha)
    submit(db, code_reviewer, frontend, "review", "rejected", 1, sha=frontend_sha)
    await hand_off(world, team, frontend, frontend_dev, primary)
    frontend_fix = commit_work(db, tree, frontend, "frontend_fix.py")
    submit(db, primary, frontend, "implementation", "approved", 2, sha=frontend_fix)
    submit(db, code_reviewer, frontend, "review", "approved", 2, sha=frontend_fix)
    submit(db, code_reviewer, wire, "review", "approved", 1, sha=wire_sha)
    submit(db, code_reviewer, retire, "review", "approved", 1, disposition="moot")
    submit(db, docs_reviewer, document, "review", "approved", 1, sha=doc_sha)
    submit(db, research_reviewer, survey, "review", "approved", 1, sha=survey_sha)

    state = sync_epic_review(db, team.binding.team_id)
    assert not state.held and not state.epic_tip_ready
    assert state.next_reviewers == {
        backend.id: "code-reviewer",
        frontend.id: "code-reviewer",
        wire.id: "code-reviewer",
        retire.id: "code-reviewer",
        document.id: "docs-reviewer",
        survey.id: "research-reviewer",
    }

    # The 0.5.0 merge lands in epic main before the epic-tip review (Decision 19).
    world.case.direct({"release/0.5.0.txt": "Released while the epic ran.\n"})
    git_in(tree, "merge", "--no-ff", "trunk", "-m", "Merge 0.5.0 into epic main")
    tip = git_in(tree, "rev-parse", "HEAD")
    tasks.link_commit(epic.id, tip)
    tip_reviewer = team.seat("code-reviewer", "review")
    assert sync_epic_review(db, team.binding.team_id).epic_tip_ready
    submit(db, tip_reviewer, epic, "review", "approved", 1, sha=tip)

    # Only the Dev Manager lands; the epic-tip reviewer may not.
    refused = await world.case.land(tasks.get_task(epic.id), tip, caller=tip_reviewer)
    assert refused["landed"] is False, refused
    assert "caller_not_task_manager" in refused["blockers"], refused
    landed = await world.case.land(tasks.get_task(epic.id), tip, caller=manager)
    assert (landed["landed"], landed["worktree_merged"]) == (True, True), landed
    assert world.case.git("rev-parse", "trunk") == tip

    # Leaves close bottom-up: no-work first, then work-bearing; the phase epic closes by cascade.
    submit(db, manager, retire, "closing", "approved", 1, disposition="moot")
    tasks.close_task(retire.id, reason="moot", closed_in_session_id=manager)
    for leaf, sha in ((backend, backend_sha), (frontend, frontend_fix)):
        submit(db, manager, leaf, "closing", "approved", 1, sha=sha)
        tasks.close_task(
            leaf.id, reason="done", closed_in_session_id=manager, closed_commit_sha=sha
        )
    submit(db, manager, wire, "closing", "approved", 1, sha=wire_sha)
    tasks.close_task(
        wire.id, reason="done", closed_in_session_id=manager, closed_commit_sha=wire_sha
    )
    assert tasks.get_task(phase.id).closed_at is not None
    assert tasks.get_task(epic.id).closed_at is None
    # A leaf's close queues no teardown while the bound epic is open.
    leaf_closed = closed_at(tasks.get_task(wire.id))
    assert await teardown_pass(db, daemon(db), leaf_closed + TEARDOWN_GRACE) == 0
    for leaf, sha in ((document, doc_sha), (survey, survey_sha)):
        submit(db, manager, leaf, "closing", "approved", 1, sha=sha)
        tasks.close_task(
            leaf.id, reason="done", closed_in_session_id=manager, closed_commit_sha=sha
        )
    tasks.close_task(epic.id, reason="done", closed_in_session_id=manager, closed_commit_sha=tip)

    # Exactly one teardown runs, keyed on the bound epic's close.
    epic_closed = closed_at(tasks.get_task(epic.id))
    runner = daemon(db)
    assert await teardown_pass(db, runner, epic_closed) == 0
    assert await teardown_pass(db, runner, epic_closed + TEARDOWN_GRACE) > 0
    assert await teardown_pass(db, runner, epic_closed + TEARDOWN_GRACE) == 0
    assert teardown_outcome(world, team.runbook) == {"outcome": "cleaned"}
    assert manager in terminations.ended()
    assert session_status(db, manager) == "expired"
    assert not tree.exists()
    assert LocalWorktreeManager(db).get(team.binding.worktree_id) is None
    assert not branch_exists(world, team.binding.branch)

    # The epic folder's final commit lands in the workspace repository.
    await drain_workspace_commits()
    subjects = workspace_subjects(world.case.project_id)
    assert any(f"epic-{epic.seq_num}" in subject for subject in subjects)


@pytest.mark.asyncio
async def test_escalated_leaf_holds_then_lands(world: TeamWorld) -> None:
    """A third leaf rejection holds the epic until Triage's audited reset, then it lands (8.6.2)."""
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

    # Three implementation rounds, each rejected: the third rejection escalates the leaf.
    tasks.claim_task(stuck.id, developer, force=True)
    for number in (1, 2, 3):
        sha = commit_work(db, tree, stuck, f"stuck_round_{number}.py")
        submit(db, developer, stuck, "implementation", "approved", number, sha=sha)
        submit(db, reviewer, stuck, "review", "rejected", number, sha=sha)
    assert tasks.get_task(stuck.id).escalated_at is not None

    tasks.claim_task(done.id, developer, force=True)
    done_sha = commit_work(db, tree, done, "done.py")
    submit(db, developer, done, "implementation", "approved", 1, sha=done_sha)

    # The escalated leaf holds the epic: the epic escalates once, and dispatch stops.
    state = sync_epic_review(db, team.binding.team_id)
    assert state.held and stuck.id in state.holds
    held_epic = tasks.get_task(epic.id)
    assert held_epic.escalated_at is not None
    assert held_epic.claimed_by_session_id is None
    assert stuck.id in (held_epic.escalation_reason or "")
    assert sync_epic_review(db, team.binding.team_id).held
    before = len(world.launches)
    refused = await team.spawn("developer", task_id=waiting.id, checkout_mode="worktree")
    assert refused.get("success") is not True, refused
    assert len(world.launches) == before

    # Nothing lands while held: no epic-tip approval can exist before every leaf is approved.
    tip = git_in(tree, "rev-parse", "HEAD")
    tasks.link_commit(epic.id, tip)
    assert not sync_epic_review(db, team.binding.team_id).epic_tip_ready
    held_land = await world.case.land(tasks.get_task(epic.id), tip, caller=manager)
    assert held_land["landed"] is False, held_land
    assert "review_approval_missing" in held_land["blockers"], held_land

    # Triage's audited reset starts a new review epoch for the leaf and de-escalates the epic.
    epoch = tasks.get_task(stuck.id).review_epoch
    tasks.de_escalate_task(stuck.id, reason="Triage audited reset")
    assert tasks.get_task(stuck.id).review_epoch == epoch + 1
    tasks.de_escalate_task(epic.id, reason="Triage audited reset")
    lifted = sync_epic_review(db, team.binding.team_id)
    assert (lifted.held, lifted.holds) == (False, ())
    allowed = await team.spawn("developer", task_id=waiting.id, checkout_mode="worktree")
    assert allowed.get("success") is True, allowed

    # A held team refuses new review rounds, so the finished leaf is reviewed once the hold lifts.
    submit(db, reviewer, done, "review", "approved", 1, sha=done_sha)

    # The reset leaf is worked again in its new epoch, and the waiting leaf finishes.
    tasks.claim_task(stuck.id, developer, force=True)
    fix = commit_work(db, tree, stuck, "stuck_fix.py")
    submit(db, developer, stuck, "implementation", "approved", 1, sha=fix)
    submit(db, reviewer, stuck, "review", "approved", 1, sha=fix)
    waiting_dev = team.seat("developer", "implementation")
    tasks.claim_task(waiting.id, waiting_dev, force=True)
    waiting_sha = commit_work(db, tree, waiting, "waiting.py")
    submit(db, waiting_dev, waiting, "implementation", "approved", 1, sha=waiting_sha)
    submit(db, reviewer, waiting, "review", "approved", 1, sha=waiting_sha)

    # The epic tip is approved by the epic-tip reviewer and lands through the Dev Manager.
    tip = git_in(tree, "rev-parse", "HEAD")
    tasks.link_commit(epic.id, tip)
    tip_reviewer = team.seat("code-reviewer", "review")
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
