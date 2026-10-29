"""A team world: a landing case plus team runbook executions, admission and seat spawns."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.team_lifecycle import TeamBinding, admit_team
from gobby.code_index.gcode_gateway import GcodeGateway
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.runbook_seat_tools import register_runbook_seat_tools
from gobby.mcp_proxy.tools.spawn_agent import _factory, create_spawn_agent_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.machines import LocalMachineManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import Task
from gobby.storage.team_records import ADMISSION_STEP
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import get_current_session_id, session_context_for_test
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.landing_case import LandingCase
from tests.fixtures.postgres import TEST_USER_ID


def synthetic_manager_body() -> dict[str, Any]:
    """A synthetic team manager: the team_manager grant and the spawn scope the tests exercise."""
    return {
        "name": "dev-manager",
        "version": "1.0",
        "surfaces": ["spawn"],
        "send_message_targets": ["parent", "session", "project"],
        "spawnable_agents": ["developer", "code-reviewer", "docs-reviewer", "research-reviewer"],
        "prompts": {"agent": "Run the dev-manager seat."},
        "workflows": {
            "rule_selectors": {"include": []},
            "variables": {"team_manager": True},
        },
        "task_step_writes": ["closing"],
    }


TITLE = "Scroll and type during daemon outage window"
# The spawn default slugs the title and truncates the slug to 40 characters.
SLUG = "scroll-and-type-during-daemon-outage-win"
RUNBOOK = PipelineDefinition(
    name="team-runbook",
    steps=[PipelineStep(id=ADMISSION_STEP, exec="true"), PipelineStep(id="developer", exec="true")],
)


def git_in(cwd: str | Path, *args: str) -> str:
    """Run git in ``cwd`` without the user's global or system config."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.strip()


def execution_id(child: Session) -> str:
    return child.external_id.removeprefix("pipeline-")


@dataclass
class TeamWorld:
    case: LandingCase
    spawns: InternalToolRegistry
    tools: InternalToolRegistry
    launches: list[dict[str, Any]] = field(default_factory=list)
    # Replies the next spawns return in place of a launch, for a spawn that fails.
    spawn_replies: list[dict[str, Any]] = field(default_factory=list)

    @property
    def executions(self) -> LocalPipelineExecutionManager:
        return LocalPipelineExecutionManager(self.case.db, self.case.project_id)

    def child(
        self, task: Task, definition: PipelineDefinition = RUNBOOK, *, parent: str | None = None
    ) -> Session:
        """Start a running team runbook execution for ``task`` whose admit step is running."""
        execution = self.executions.create_execution(
            definition.name,
            json.dumps({"task": task.id}),
            definition_json=definition.model_dump_json(),
            project_id=self.case.project_id,
        )
        child = SessionManager(self.case.db).register(
            external_id=f"pipeline-{execution.id}",
            machine_id=require_machine_id(),
            source="pipeline",
            project_id=self.case.project_id,
            parent_session_id=parent or self.case.creator,
        )
        self.executions.update_execution_session(execution.id, child.id)
        self.executions.update_execution_status(execution.id, ExecutionStatus.RUNNING)
        step = self.executions.create_step_execution(execution.id, ADMISSION_STEP)
        self.executions.update_step_execution(step.id, status=StepStatus.RUNNING)
        return child

    def admit_step_output(self, child: Session) -> dict[str, Any]:
        (step,) = (
            step
            for step in self.executions.get_steps_for_execution(execution_id(child))
            if step.step_id == ADMISSION_STEP
        )
        output: dict[str, Any] = json.loads(step.output_json or "{}")
        return output

    def rewrite_admit_output(self, child: Session, output: dict[str, Any]) -> None:
        (step,) = (
            step
            for step in self.executions.get_steps_for_execution(execution_id(child))
            if step.step_id == ADMISSION_STEP
        )
        self.executions.update_step_execution(step.id, output_json=json.dumps(output))

    def finish(self, child: Session) -> None:
        """Complete the execution as a fire-and-forget runbook does once its seats launch."""
        for step in self.executions.get_steps_for_execution(execution_id(child)):
            self.executions.update_step_execution(step.id, status=StepStatus.COMPLETED)
        self.executions.update_execution_status(execution_id(child), ExecutionStatus.COMPLETED)

    def fail(self, child: Session) -> None:
        """Fail the execution as a runbook does when a seat step raises before it launches."""
        for step in self.executions.get_steps_for_execution(execution_id(child)):
            self.executions.update_step_execution(step.id, status=StepStatus.FAILED)
        self.executions.update_execution_status(execution_id(child), ExecutionStatus.FAILED)

    async def admit(
        self,
        caller: Session,
        task: Task,
        team_id: str | None = None,
        *,
        fresh_reviewer: bool = False,
        exclude_reviewers: Sequence[str] = (),
        checkout_mode: str = "worktree",
        parent_team_id: str | None = None,
    ) -> TeamBinding:
        return await admit_team(
            self.case.db,
            caller_session_id=caller.id,
            task=task.id,
            team_id=team_id,
            parent_team_id=parent_team_id,
            checkout_mode=checkout_mode,
            fresh_reviewer=fresh_reviewer,
            exclude_reviewers=exclude_reviewers,
        )

    async def spawn(self, caller: Session, **target: Any) -> dict[str, Any]:
        """Spawn the caller runbook's developer seat through spawn_agent."""
        arguments = {
            "prompt": "Run the developer seat",
            "parent_session_id": caller.id,
            "reserved_run_id": step_invocation_id(execution_id(caller), "developer"),
            **target,
        }
        with session_context_for_test(caller.id):
            reply: dict[str, Any] = await self.spawns.call("spawn_agent", arguments)
        return reply

    async def direct_spawn(self, session_id: str, **target: Any) -> dict[str, Any]:
        """Spawn an agent straight from an ordinary session, outside any runbook."""
        arguments = {"prompt": "Work the task", **target}
        with session_context_for_test(session_id):
            reply: dict[str, Any] = await self.spawns.call("spawn_agent", arguments)
        return reply

    def task_branches(self) -> list[str]:
        return self.case.git(
            "for-each-ref", "--format=%(refname:short)", "refs/heads/task-*"
        ).split()

    def tree_ids(self) -> list[str]:
        trees = LocalWorktreeManager(self.case.db).list_worktrees(project_id=self.case.project_id)
        return [tree.id for tree in trees]


def running_seat(world: TeamWorld, runbook: Session, agent_type: str) -> Session:
    """A seat ``runbook`` launched as ``agent_type``, with its running run bound to it."""
    db = world.case.db
    child = world.case.session(agent_type, parent=runbook.id)
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent_session_id=runbook.id,
        provider="claude",
        prompt=f"Run the {agent_type} seat.",
        agent_name=agent_type,
        child_session_id=child,
    )
    started = runs.start(run.id)
    assert started is not None and started.status == "running"
    SessionVariableManager(db).merge_variables(child, {"_agent_type": agent_type})
    seat = SessionManager(db).update_terminal_pickup_metadata(child, agent_run_id=run.id)
    assert seat is not None and seat.agent_run_id == run.id
    return seat


def make_team_world(
    case: LandingCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TeamWorld:
    """Wire spawn_agent and the runbook tools over ``case``; launches are recorded, not run."""
    # Team trees live under ~/.gobby/worktrees; keep them inside the test's directory.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(GcodeGateway, "pin_project", AsyncMock())
    LocalMachineManager(case.db).upsert_seen(require_machine_id(), TEST_USER_ID, hostname="local")
    world = TeamWorld(
        case=case,
        spawns=create_spawn_agent_registry(
            MagicMock(), session_manager=SessionManager(case.db), db=case.db
        ),
        tools=InternalToolRegistry(name="test-agents", description="test"),
    )
    register_runbook_seat_tools(
        world.tools,
        AgentsRegistryContext(
            runner=MagicMock(),
            agent_run_manager=LocalAgentRunManager(case.db),
            resolve_session_id=lambda ref: ref,
            get_current_session_id=get_current_session_id,
            get_current_agent_run_id=lambda: None,
            get_project_context=lambda: None,
            db=case.db,
        ),
    )

    async def launch(**kwargs: Any) -> dict[str, Any]:
        """The ordinary spawn path: record the launch the team check let through."""
        if world.spawn_replies:
            return world.spawn_replies.pop(0)
        world.launches.append(kwargs)
        return {"success": True, "run_id": kwargs["reserved_run_id"]}

    context = {"id": case.project_id, "project_path": str(case.repo)}
    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)
    monkeypatch.setattr(_factory, "get_project_context", lambda *a, **k: context)
    monkeypatch.setattr(_factory, "_load_agent_body", lambda *a, **k: None)
    return world
