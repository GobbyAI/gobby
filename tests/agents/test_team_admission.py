"""Single-use team admission: one task, one task-titled tree, bound to the admitting execution."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.agents.team_lifecycle import (
    ADMISSION_STEP,
    TeamAdmissionRefused,
    TeamBinding,
    admit_team,
)
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.runbook_seat_tools import register_runbook_seat_tools
from gobby.mcp_proxy.tools.spawn_agent import _factory, create_spawn_agent_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import get_current_session_id, session_context_for_test
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus
from tests.fixtures.landing_case import LandingCase, make_landing_case
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

TITLE = "Scroll and type during daemon outage window"
# The spawn default slugs the title and truncates the slug to 40 characters.
SLUG = "scroll-and-type-during-daemon-outage-win"
RUNBOOK = PipelineDefinition(
    name="team-runbook",
    steps=[PipelineStep(id=ADMISSION_STEP, exec="true"), PipelineStep(id="developer", exec="true")],
)


def _git(cwd: str | Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.strip()


def _execution_id(child: Session) -> str:
    return child.external_id.removeprefix("pipeline-")


@dataclass
class _World:
    case: LandingCase
    spawns: InternalToolRegistry
    tools: InternalToolRegistry
    launches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def executions(self) -> LocalPipelineExecutionManager:
        return LocalPipelineExecutionManager(self.case.db, self.case.project_id)

    def child(self, task: Task) -> Session:
        """Start a running team runbook execution for ``task`` whose admit step is running."""
        execution = self.executions.create_execution(
            RUNBOOK.name,
            json.dumps({"task": task.id}),
            definition_json=RUNBOOK.model_dump_json(),
            project_id=self.case.project_id,
        )
        child = SessionManager(self.case.db).register(
            external_id=f"pipeline-{execution.id}",
            machine_id=require_machine_id(),
            source="pipeline",
            project_id=self.case.project_id,
            parent_session_id=self.case.creator,
        )
        self.executions.update_execution_session(execution.id, child.id)
        self.executions.update_execution_status(execution.id, ExecutionStatus.RUNNING)
        step = self.executions.create_step_execution(execution.id, ADMISSION_STEP)
        self.executions.update_step_execution(step.id, status=StepStatus.RUNNING)
        return child

    def admit_step_output(self, child: Session) -> dict[str, Any]:
        (step,) = (
            step
            for step in self.executions.get_steps_for_execution(_execution_id(child))
            if step.step_id == ADMISSION_STEP
        )
        output: dict[str, Any] = json.loads(step.output_json or "{}")
        return output

    def rewrite_admit_output(self, child: Session, output: dict[str, Any]) -> None:
        (step,) = (
            step
            for step in self.executions.get_steps_for_execution(_execution_id(child))
            if step.step_id == ADMISSION_STEP
        )
        self.executions.update_step_execution(step.id, output_json=json.dumps(output))

    def finish(self, child: Session) -> None:
        """Complete the execution as a fire-and-forget runbook does once its seats launch."""
        for step in self.executions.get_steps_for_execution(_execution_id(child)):
            self.executions.update_step_execution(step.id, status=StepStatus.COMPLETED)
        self.executions.update_execution_status(_execution_id(child), ExecutionStatus.COMPLETED)

    async def admit(self, caller: Session, task: Task, team_id: str | None = None) -> TeamBinding:
        return await admit_team(
            self.case.db, caller_session_id=caller.id, task=task.id, team_id=team_id
        )

    async def spawn(self, caller: Session, **target: Any) -> dict[str, Any]:
        """Spawn the caller runbook's developer seat through spawn_agent."""
        arguments = {
            "prompt": "Run the developer seat",
            "parent_session_id": caller.id,
            "reserved_run_id": step_invocation_id(_execution_id(caller), "developer"),
            **target,
        }
        with session_context_for_test(caller.id):
            reply: dict[str, Any] = await self.spawns.call("spawn_agent", arguments)
        return reply

    def task_branches(self) -> list[str]:
        return self.case.git(
            "for-each-ref", "--format=%(refname:short)", "refs/heads/task-*"
        ).split()

    def tree_ids(self) -> list[str]:
        trees = LocalWorktreeManager(self.case.db).list_worktrees(project_id=self.case.project_id)
        return [tree.id for tree in trees]


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _World:
    # Team trees live under ~/.gobby/worktrees; keep them inside the test's directory.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    LocalMachineManager(temp_db).upsert_seen(require_machine_id(), TEST_USER_ID, hostname="local")
    case = make_landing_case(temp_db, sample_project, tmp_path)
    world = _World(
        case=case,
        spawns=create_spawn_agent_registry(
            MagicMock(), session_manager=SessionManager(temp_db), db=temp_db
        ),
        tools=InternalToolRegistry(name="test-agents", description="test"),
    )
    register_runbook_seat_tools(
        world.tools,
        AgentsRegistryContext(
            runner=MagicMock(),
            agent_run_manager=LocalAgentRunManager(temp_db),
            resolve_session_id=lambda ref: ref,
            get_current_session_id=get_current_session_id,
            get_current_agent_run_id=lambda: None,
            get_project_context=lambda: None,
            db=temp_db,
        ),
    )

    async def launch(**kwargs: Any) -> dict[str, Any]:
        """The ordinary spawn path: record the launch the team check let through."""
        world.launches.append(kwargs)
        return {"success": True, "run_id": kwargs["reserved_run_id"]}

    context = {"id": case.project_id, "project_path": str(case.repo)}
    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)
    monkeypatch.setattr(_factory, "get_project_context", lambda *a, **k: context)
    monkeypatch.setattr(_factory, "_load_agent_body", lambda *a, **k: None)
    return world


async def test_idempotent_team_admission(world: _World) -> None:
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
    winner, loser = (first, second) if team.team_id == _execution_id(first) else (second, first)
    assert team.team_id == _execution_id(winner)

    branch = f"task-{task.seq_num}-{SLUG}"
    assert (team.task_id, team.project_id, team.branch, team.base_branch) == (
        task.id,
        world.case.project_id,
        branch,
        "trunk",
    )
    assert Path(team.worktree_path).name == branch
    assert _git(team.worktree_path, "branch", "--show-current") == branch
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


async def test_replacement_stays_in_admitted_team(world: _World) -> None:
    task = world.case.task(TITLE)
    original = world.child(task)
    team = await world.admit(original, task)
    tree = Path(team.worktree_path)
    (tree / "work.txt").write_text("committed by the developer\n")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "-m", "developer work")
    head = _git(tree, "rev-parse", "HEAD")
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
    assert _git(tree, "rev-parse", "HEAD") == head
    assert (tree / "draft.txt").read_text() == "uncommitted draft\n"
    assert world.tree_ids() == [team.worktree_id]
    assert world.task_branches() == [team.branch]


async def test_team_binding_cannot_drift(world: _World, tmp_path: Path) -> None:
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
        (lambda: world.admit(owner, task, team_id=_execution_id(stranger)), "team_binding_drift"),
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
    assert sorted(world.tree_ids()) == sorted([team.worktree_id, elsewhere.id])
