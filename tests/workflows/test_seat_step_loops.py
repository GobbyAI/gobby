"""Loop seats run consecutive units of work through the real step engine (plan 3.5.5).

Each driver loads a bundled seat's step workflow into an agent-step instance,
fires the hook events its session would produce, and checks the step it lands
on, the tools that step admits, and the correlation variables its handlers bind
and reset between units.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import uuid
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.agents.runner import AgentRunner
from gobby.agents.sync import get_bundled_agents_path
from gobby.agents.team_lifecycle import ADMISSION_STEP, TeamBinding
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_commits import create_commit_registry
from gobby.servers.routes.mcp.endpoints.execution import _process_tool_proxy_result
from gobby.skills.instruction_requirements import parse_instruction_requirement
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.workflow_audit import WorkflowAuditManager
from gobby.utils.git import normalize_commit_sha
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import PipelineDefinition, PipelineStep, RuleDefinitionBody
from gobby.workflows.engine import seat_transitions
from gobby.workflows.engine._offload import ENGINE_EXECUTOR_THREAD_PREFIX, RULE_LOOP_THREAD_PREFIX
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import (
    AgentStepInstance,
    AgentStepInstanceManager,
    StaleStepInstanceWriteError,
)
from tests.fixtures.isolated_checkout import (
    install_isolated_checkout_project,
    patch_local_machine_id,
)
from tests.fixtures.review_approvals import approve_review

pytestmark = pytest.mark.unit

_CANDIDATE_DEFAULTS = {
    "candidate_received": False,
    "verdict_ready": False,
    "candidate_task": None,
    "candidate_sha": None,
    "candidate_owner": None,
    "candidate_received_at": None,
}

_CANDIDATE_GRAMMAR = (
    "CANDIDATE not bound: expected EVENT=CANDIDATE TASK=<ref> [TASK_TITLE=...] "
    "RUN= WT= COMMIT=<40-hex> [NOTE=...]"
)

# Session and project ids are native uuid columns in PostgreSQL.
SESSION_ID = "33333333-3333-4333-8333-333333333333"
PROJECT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
AGENTS_DIR = get_bundled_agents_path()

OWNER_ID = "55555555-5555-4555-8555-555555555555"
COORDINATOR_ID = "66666666-6666-4666-8666-666666666666"
PEER_ID = "77777777-7777-4777-8777-777777777777"
AUTHORITY_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
SHA = "a" * 40
REPLACEMENT_SHA = "b" * 40
WAIT = ("gobby-agents", "wait_for_coordination")
SEND = ("gobby-agents", "send_message")
DERIVE = ("gobby-plans", "derive_plan_handoff_manifest")
APPLY = ("gobby-plans", "apply_plan_handoff_manifest")
EDIT_INPUT = {"file_path": "src/gobby/example.py", "old_string": "a", "new_string": "b"}


class SeatRun:
    """One bundled seat's agent-step instance, driven by hook events."""

    def __init__(self, db: HubDatabase, name: str, *, project_id: str = PROJECT_ID) -> None:
        self.db = db
        self.project_id = project_id
        self.sessions = SessionManager(db)
        self.engine = RuleEngine(
            db, session_manager=self.sessions, task_manager=LocalTaskManager(db)
        )
        self.instances = AgentStepInstanceManager(db)
        self.session_vars: dict[str, Any] = {
            "loaded_skills": [],
            "loaded_skill_references": [],
            "_agent_type": name,
        }

        agent = yaml.safe_load((AGENTS_DIR / f"{name}.yaml").read_text())
        workflow = agent["step_workflow"]
        db.execute(
            "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP) "
            "ON CONFLICT (id) DO NOTHING",
            (project_id, "seat-loops"),
        )
        db.execute(
            "INSERT INTO sessions "
            "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (SESSION_ID, "ext-seat", "21000000-0000-4000-8000-000000000001", "claude", project_id),
        )
        for other_id, role in (
            (OWNER_ID, "developer"),
            (COORDINATOR_ID, "orchestrator"),
            (PEER_ID, "code-reviewer"),
        ):
            db.execute(
                "INSERT INTO sessions "
                "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (other_id, other_id, "21000000-0000-4000-8000-000000000001", "claude", project_id),
            )
            SessionVariableManager(db).set_variable(other_id, "_agent_type", role)
        SessionVariableManager(db).set_variable(SESSION_ID, "_agent_type", name)
        # network is sync-owned and refused on create; the step workflow never reads it.
        definition = {key: value for key, value in agent.items() if key != "network"}
        AgentDefinitionManager(db).create(
            name=name,
            definition_json=json.dumps(definition),
            enabled=True,
        )
        # The coordinator's review grant lets negative cases store its approvals.
        AgentDefinitionManager(db).create(
            "orchestrator",
            {
                "name": "orchestrator",
                "version": "1.0",
                "prompts": {"agent": "Coordinate the seat protocol."},
                "workflows": {"rule_selectors": {"include": []}},
                "task_step_writes": ["review"],
            },
            project_id=project_id,
        )
        self.instances.save(
            AgentStepInstance(
                id=str(uuid.uuid4()),
                session_id=SESSION_ID,
                agent_name=name,
                snapshot=AgentStepWorkflowBody.model_validate(workflow),
                enabled=True,
                current_step=workflow["steps"][0]["name"],
                step_entered_at=datetime.now(UTC),
                variables=dict(workflow["variables"]),
            )
        )

    @property
    def step(self) -> str | None:
        instance = self.instances.get_for_session(SESSION_ID)
        assert instance is not None
        return instance.current_step

    @property
    def vars(self) -> dict[str, Any]:
        instance = self.instances.get_for_session(SESSION_ID)
        assert instance is not None
        return instance.variables

    def _event(self, event_type: HookEventType, data: dict[str, Any]) -> HookEvent:
        return HookEvent(
            event_type=event_type,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data=normalize_tool_fields(data),
            metadata={},
        )

    async def _evaluate(self, event: HookEvent) -> str:
        response = await self.engine.evaluate(
            event, session_id=SESSION_ID, variables=self.session_vars
        )
        return response.decision

    async def admits(self, server: str, tool: str, arguments: dict[str, Any] | None = None) -> bool:
        call = {"server_name": server, "tool_name": tool, "arguments": arguments or {}}
        data = {"tool_name": "mcp__gobby__call_tool", "tool_input": call}
        return await self._evaluate(self._event(HookEventType.BEFORE_TOOL, data)) != "block"

    async def admits_native(self, tool: str, tool_input: dict[str, Any]) -> bool:
        data = {"tool_name": tool, "tool_input": tool_input}
        return await self._evaluate(self._event(HookEventType.BEFORE_TOOL, data)) != "block"

    async def call(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        output: dict[str, Any] | None = None,
    ) -> None:
        """Report one successful MCP call the step admits."""
        assert await self.admits(server, tool, arguments), (self.step, server, tool)
        call = {"server_name": server, "tool_name": tool, "arguments": arguments}
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": call,
            "tool_output": output if output is not None else {"success": True},
        }
        await self._evaluate(self._event(HookEventType.AFTER_TOOL, data))

    async def load(self, *entries: str) -> None:
        """Load skills and skill references, recording each in its session ledger."""
        for entry in entries:
            requirement = parse_instruction_requirement(entry)
            if requirement.path is None:
                self.session_vars["loaded_skills"].append(requirement.identity)
                await self.call("gobby-skills", "get_skill", {"name": requirement.skill})
            else:
                self.session_vars["loaded_skill_references"].append(requirement.identity)
                arguments = {"name": requirement.skill, "path": requirement.path}
                await self.call("gobby-skills", "get_skill_file", arguments)

    def task(self, *, owner: str = OWNER_ID, skills: list[str] | None = None) -> Task:
        return LocalTaskManager(self.db).create_task(
            project_id=self.project_id,
            title="Seat protocol unit",
            validation_criteria="The seat advances only after its stored protocol event is verified.",
            created_in_session_id=COORDINATOR_ID,
            claimed_by_session_id=owner,
            additional_skills=skills,
        )

    async def send(self, content: str, recipient: str = COORDINATOR_ID) -> None:
        message = InterSessionMessageManager(self.db).create_message(SESSION_ID, recipient, content)
        await self.call(
            *SEND,
            {"target": "session", "target_id": recipient, "content": content},
            {"success": True, "message_ids": [message.id]},
        )

    async def incoming(self, content: str, sender: str = OWNER_ID) -> str:
        message = InterSessionMessageManager(self.db).create_message(sender, SESSION_ID, content)
        await self.call(
            "gobby-agents",
            "get_inter_session_message",
            {"message_id": message.id},
            {"success": True, "message": message.to_dict()},
        )
        return message.id

    async def intake(self, task: Task, sha: str = SHA) -> None:
        await self.incoming(
            f"LANE=1 EVENT=CANDIDATE TASK={task.id} RUN=run WT=tree COMMIT={sha} NOTE=ready"
        )

    def record_approval(self, task: Task, sha: str = SHA, author: str = SESSION_ID) -> None:
        """Store the author's review LAND of the newly linked tip."""
        SessionTaskManager(self.db).link_task(SESSION_ID, task.id, "reviewing")
        LocalTaskManager(self.db).link_commit(task.id, sha)
        approve_review(self.db, task_id=task.id, reviewer_session_id=author, commit_sha=sha)

    async def approve(self, task: Task, sha: str = SHA, author: str = SESSION_ID) -> None:
        self.record_approval(task, sha, author)
        await self.call(
            "gobby-tasks",
            "get_task",
            {"task_id": task.id},
            {"result": {"id": task.id, "ref": f"#{task.seq_num}"}},
        )


async def _developer(run: SeatRun) -> None:
    await run.load(*run.vars["required_skills"])
    assert run.step == "claim"
    for skill in ("rust", "typescript"):
        task = run.task(owner=SESSION_ID, skills=[skill])
        ref = f"#{task.seq_num}"
        await run.call(
            "gobby-tasks",
            "claim_task",
            {"task_id": task.id},
            {"result": {"task_id": task.id, "ref": ref}},
        )
        assert run.step == "route_skills"
        await run.call(
            "gobby-tasks", "get_task", {"task_id": task.id}, {"result": {"id": task.id, "ref": ref}}
        )
        assert run.step == "load_additional_skills"
        assert run.vars["additional_skills"] == [skill]
        assert not await run.admits_native("Edit", EDIT_INPUT)
        await run.load(skill)
        assert run.step == "implement"
        assert await run.admits_native("Edit", EDIT_INPUT)
        await run.call("gobby-tasks", "link_commit", {"task_id": task.id, "commit_sha": SHA})
        assert run.step == "submit"
        await run.send(f"EVENT=CANDIDATE TASK={ref} RUN=run WT=tree COMMIT={SHA} NOTE=ready")
        assert run.vars["candidate_sent"] is True
        assert not await run.admits_native(
            "mcp__gobby__set_variable",
            {
                "name": "candidate_sent",
                "value": False,
                "session_id": SESSION_ID,
                "scope": "step",
            },
        )
        assert run.vars["candidate_sent"] is True
        await run.call("gobby-tasks", "close_task", {"task_id": task.id}, {"closed": True})
        assert run.step == "claim"
        assert run.vars["assigned_task_id"] is None
        assert run.vars["candidate_sent"] is False
        assert run.vars["skills_routed"] is False


async def _code_reviewer(run: SeatRun) -> None:
    await run.load(*run.vars["required_skills"])
    for _ in range(2):
        task = run.task()
        await run.intake(task)
        assert run.step == "review"
        assert run.vars["candidate_task"] == task.id
        await run.approve(task)
        assert run.step == "verdict"
        await run.send(f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={SHA} VERDICT=LAND NOTE=ok")
        assert run.step == "await"
        assert run.vars["candidate_received"] is False
        assert run.vars["candidate_sha"] is None


@pytest.mark.parametrize("name", ["developer", "code-reviewer"])
async def test_loop_seats_reset_between_units(temp_db: HubDatabase, name: str) -> None:
    run = SeatRun(temp_db, name)
    if name == "developer":
        await _developer(run)
    else:
        await _code_reviewer(run)
    assert run.step == ("claim" if name == "developer" else "await")


@pytest.mark.parametrize("trigger", ["get_task", "record_review_step"])
@pytest.mark.parametrize(
    "author,sha,ready",
    [
        (SESSION_ID, SHA, True),
        (PEER_ID, SHA, False),
        (COORDINATOR_ID, SHA, False),
        (SESSION_ID, REPLACEMENT_SHA, False),
    ],
)
async def test_reviewer_own_review_record_controls_verdict_readiness(
    temp_db: HubDatabase, trigger: str, author: str, sha: str, ready: bool
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    run.record_approval(task, sha, author)
    await run.call(
        "gobby-tasks",
        trigger,
        {"task_id": task.id, "status": "approved", "commit_sha": sha},
        {"success": True},
    )
    assert run.step == ("verdict" if ready else "review")
    # The verdict is the reviewer's whole output; the Orchestrator lands it.
    assert not await run.admits(
        "gobby-tasks-ops", "land_commit", {"task_id": task.id, "commit_sha": SHA}
    )


@pytest.mark.parametrize(
    ("author", "sha", "clears"),
    [(SESSION_ID, SHA, True), (PEER_ID, SHA, False), (SESSION_ID, REPLACEMENT_SHA, False)],
)
async def test_reviewer_land_verdict_clears_only_with_own_review_record(
    temp_db: HubDatabase, author: str, sha: str, clears: bool
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    run.record_approval(task, sha, author)
    # A stale verdict flag cannot stand in for the stored approval.
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    instance.current_step = "verdict"
    instance.variables["verdict_ready"] = True
    run.instances.save(instance)
    await run.send(f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={SHA} VERDICT=LAND")
    assert run.step == ("await" if clears else "verdict")
    assert run.vars["candidate_received"] is not clears


@pytest.mark.parametrize("forgery", ["outgoing", "self", "missing-row", "wrong-owner", "short-sha"])
async def test_candidate_intake_rejects_forged_authority(
    temp_db: HubDatabase, forgery: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    line = f"EVENT=CANDIDATE TASK={task.id} RUN=run WT=tree COMMIT={SHA} NOTE=ready"
    if forgery == "outgoing":
        await run.send(line)
    elif forgery == "self":
        await run.incoming(line, SESSION_ID)
    elif forgery == "missing-row":
        await run.call(
            "gobby-agents",
            "get_inter_session_message",
            {"message_id": str(uuid.uuid4())},
            {"success": True, "message": {"content": line, "from_session": OWNER_ID}},
        )
    elif forgery == "wrong-owner":
        await run.incoming(line, COORDINATOR_ID)
    else:
        await run.incoming(line.replace(SHA, "abc1234"))
    assert run.step == "await"
    assert run.vars["candidate_received"] is False


async def test_replacement_requires_fresh_approval_and_does_not_take_other_task(
    temp_db: HubDatabase,
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task, queued = run.task(), run.task()
    await run.intake(task)
    await run.approve(task)
    assert run.step == "verdict"
    await run.intake(queued)
    assert run.vars["candidate_task"] == task.id
    await run.intake(task, REPLACEMENT_SHA)
    assert run.step == "review"
    assert run.vars["candidate_sha"] == REPLACEMENT_SHA
    await run.call("gobby-tasks", "get_task", {"task_id": task.id})
    assert run.step == "review"
    await run.approve(task, REPLACEMENT_SHA)
    assert run.step == "verdict"


async def test_bounce_uses_current_unit_durable_outgoing_message(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    await run.send(f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={REPLACEMENT_SHA} VERDICT=BOUNCE")
    assert run.step == "review"
    await run.send(f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={SHA} VERDICT=BOUNCE", SESSION_ID)
    assert run.step == "review"
    await run.send(f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={SHA} VERDICT=BOUNCE")
    assert run.step == "await"
    assert "parked_candidates" not in run.vars


@pytest.mark.parametrize("event", ["CANDIDATE_PARKED", "CANDIDATE_RESUMED"])
async def test_retired_park_and_resume_events_leave_the_unit_alone(
    temp_db: HubDatabase, event: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    await run.approve(task)
    before = run.vars
    await run.send(f"EVENT={event} TASK={task.id} SHA={SHA} REASON=approval")
    assert run.step == "verdict"
    assert run.vars == before


async def test_peer_park_leaves_the_unit_alone(temp_db: HubDatabase) -> None:
    """A peer reviewer's retired CANDIDATE_PARKED cannot park this seat's unit."""
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    await run.approve(task)
    before = run.vars
    SessionTaskManager(run.db).link_task(PEER_ID, task.id, "reviewing")
    await run.incoming(f"EVENT=CANDIDATE_PARKED TASK={task.id} SHA={SHA} REASON=approval", PEER_ID)
    assert run.step == "verdict"
    assert run.vars == before


async def test_coordinator_withdraws_only_the_exact_unit(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_local_machine_id(monkeypatch, "21000000-0000-4000-8000-000000000001")
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    await run.approve(task)
    wrong_sha = f"EVENT=CANDIDATE_WITHDRAWN TASK={task.id} SHA={REPLACEMENT_SHA}"
    await run.incoming(wrong_sha, COORDINATOR_ID)
    assert run.step == "verdict"
    await run.incoming(f"EVENT=CANDIDATE_WITHDRAWN TASK={task.id} SHA={SHA}", COORDINATOR_ID)
    assert run.step == "await"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repo, capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()


@pytest.fixture
async def linked_developer(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SeatRun, Task, str, str]:
    """Persist Git's real canonical-short identity through the MCP linking handler."""
    checkout = install_isolated_checkout_project(
        temp_db,
        tmp_path / "repo",
        name="seat-bounce",
        machine_id="21000000-0000-4000-8000-000000000001",
        monkeypatch=monkeypatch,
    )
    repo = Path(checkout.root_path)
    _git(repo, "init", "-q", "-b", "main")
    commit = (
        "-c",
        "user.name=Gobby Tests",
        "-c",
        "user.email=gobby-tests@example.com",
        "commit",
        "--allow-empty",
        "--no-gpg-sign",
        "-q",
        "-m",
    )
    _git(repo, *commit, "unlinked base")
    unlinked_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, *commit, "linked candidate")
    sha = _git(repo, "rev-parse", "HEAD")
    short_sha = _git(repo, "rev-parse", "--short", "HEAD")
    run = SeatRun(temp_db, "developer", project_id=checkout.project.id)
    await run.load(*run.vars["required_skills"])
    task = run.task(owner=SESSION_ID)
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": f"#{task.seq_num}"}},
    )
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, {"result": {"id": task.id}})
    assert run.step == "implement"
    registry = create_commit_registry(
        task_manager=LocalTaskManager(temp_db),
        project_manager=LocalProjectManager(temp_db),
        session_manager=run.sessions,
    )
    arguments = {"task_id": task.id, "commit_sha": sha, "project_path": str(repo)}
    with session_context_for_test(SESSION_ID):
        output = cast(dict[str, Any], await registry.call("link_commit", arguments))
    assert output == {"task_id": task.id, "commits": [short_sha]}
    assert LocalTaskManager(temp_db).get_task(task.id).commits == [short_sha]
    assert len(short_sha) < len(sha) == 40
    SessionTaskManager(run.db).link_task(PEER_ID, task.id, "reviewing")
    await run.call("gobby-tasks", "link_commit", arguments, output)
    assert run.step == "submit"
    await run.send(f"EVENT=CANDIDATE TASK=#{task.seq_num} RUN=run WT=tree COMMIT={sha} NOTE=ready")
    return run, task, sha, unlinked_sha


async def test_developer_bounce_requires_incoming_assigned_reviewer(
    linked_developer: tuple[SeatRun, Task, str, str],
) -> None:
    run, task, sha, unlinked_sha = linked_developer
    line = f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={sha} VERDICT=BOUNCE"
    await run.send(line)
    assert run.step == "submit"
    await run.incoming(line, OWNER_ID)
    assert run.step == "submit"
    other = run.task(owner=SESSION_ID)
    await run.incoming(f"EVENT=CANDIDATE_VERDICT TASK={other.id} SHA={sha} VERDICT=BOUNCE", PEER_ID)
    assert run.step == "submit"
    for rejected_sha in (unlinked_sha, SHA):
        await run.incoming(
            f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={rejected_sha} VERDICT=BOUNCE", PEER_ID
        )
        assert run.step == "submit"
    message_id = await run.incoming(line, PEER_ID)
    assert run.step == "implement"
    assert run.vars["implementation_complete"] is False
    assert run.vars["candidate_sent"] is True
    await run.call("gobby-tasks", "link_commit", {"task_id": task.id, "commit_sha": sha})
    message = InterSessionMessageManager(run.db).get_message(message_id)
    assert message is not None
    await run.call(
        "gobby-agents",
        "get_inter_session_message",
        {"message_id": message_id},
        {"success": True, "message": message.to_dict()},
    )
    assert run.step == "submit"


async def test_developer_returns_to_implement_on_security_bounce_envelope(
    linked_developer: tuple[SeatRun, Task, str, str],
) -> None:
    """The envelope a Security Reviewer sends, read by the developer at submit (#24890)."""
    run, task, sha, _ = linked_developer
    line = (
        f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={sha} VERDICT=BOUNCE "
        "TASK_TITLE=security NOTE=exploit path at src/example.py:1"
    )
    message_id = await run.incoming(line, PEER_ID)
    assert run.step == "implement"
    message = InterSessionMessageManager(run.db).get_message(message_id)
    assert message is not None
    await run.call(
        "gobby-agents",
        "get_inter_session_message",
        {"message_id": message_id},
        {"success": True, "message": message.to_dict()},
    )
    assert run.step == "implement"
    assert run.vars["bounced"] is True
    assert run.vars["implementation_complete"] is False


async def test_peer_bounce_keeps_the_code_reviewer_in_review(temp_db: HubDatabase) -> None:
    """A peer's BOUNCE leaves the Code Reviewer in review until it sends its own verdict (#24903)."""
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    assert run.vars["candidate_received"] is True
    SessionTaskManager(run.db).link_task(PEER_ID, task.id, "reviewing")
    await run.incoming(
        f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={SHA} VERDICT=BOUNCE "
        "TASK_TITLE=security NOTE=exploit path",
        PEER_ID,
    )
    assert run.step == "review"
    assert run.vars["candidate_received"] is True
    assert run.vars["candidate_sha"] == SHA


async def test_own_bounce_clears_the_code_reviewer_unit(temp_db: HubDatabase) -> None:
    """Only the Code Reviewer's own BOUNCE ends its unit and moves it to await (#24903)."""
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    assert run.vars["candidate_received"] is True
    await run.send(f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={SHA} VERDICT=BOUNCE NOTE=HIGH")
    assert run.step == "await"
    assert run.vars["candidate_received"] is False
    assert run.vars["candidate_sha"] is None


async def test_developer_submit_returns_to_implement_on_reviewer_request(
    linked_developer: tuple[SeatRun, Task, str, str],
) -> None:
    run, task, sha, _ = linked_developer
    assert not await run.admits_native("Bash", {"command": "git status"})
    await run.incoming(f"EVENT=PROOF_REQUEST TASK={task.id} SHA={sha} NOTE=evidence", PEER_ID)
    assert run.step == "implement"
    assert run.vars["implementation_complete"] is False
    assert await run.admits_native("Bash", {"command": "git status"})


async def test_developer_submit_return_refused_for_unowned_task(
    linked_developer: tuple[SeatRun, Task, str, str],
) -> None:
    run, task, sha, unlinked_sha = linked_developer
    foreign = run.task(owner=PEER_ID)
    await run.incoming(f"EVENT=PROOF_REQUEST TASK={foreign.id} SHA={sha} NOTE=evidence", PEER_ID)
    assert run.step == "submit"
    await run.incoming(
        f"EVENT=PROOF_REQUEST TASK={task.id} SHA={unlinked_sha} NOTE=evidence", PEER_ID
    )
    assert run.step == "submit"


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


async def test_developer_proof_request_matches_linked_candidate_off_the_loop(
    linked_developer: tuple[SeatRun, Task, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # advance_seat reaches _linked_candidate_matches only through the rule-engine offload, so
    # the commit identity checks run on a worker, never on the loop that delivered the message.
    run, task, sha, _ = linked_developer
    caller = asyncio.get_running_loop()
    states: list[tuple[str, asyncio.AbstractEventLoop | None]] = []
    normalize = normalize_commit_sha

    def recording(*args: Any, **kwargs: Any) -> str | None:
        states.append((threading.current_thread().name, _running_loop()))
        return normalize(*args, **kwargs)

    monkeypatch.setattr(seat_transitions, "normalize_commit_sha", recording)
    await run.incoming(f"EVENT=PROOF_REQUEST TASK={task.id} SHA={sha} NOTE=evidence", PEER_ID)
    assert run.step == "implement"
    assert states
    for name, loop in states:
        assert name.startswith((ENGINE_EXECUTOR_THREAD_PREFIX, RULE_LOOP_THREAD_PREFIX)), name
        assert loop is not caller


async def test_developer_proof_return_rearms_candidate_latch(
    linked_developer: tuple[SeatRun, Task, str, str],
) -> None:
    run, task, sha, _ = linked_developer
    await run.incoming(f"EVENT=PROOF_REQUEST TASK={task.id} SHA={sha} NOTE=evidence", PEER_ID)
    assert run.step == "implement"
    await run.call("gobby-tasks", "link_commit", {"task_id": task.id, "commit_sha": sha})
    assert run.step == "submit"
    assert run.vars["candidate_sent"] is False
    await run.send(
        f"EVENT=CANDIDATE TASK=#{task.seq_num} RUN=run WT=tree COMMIT={sha} NOTE=proof-only"
    )
    assert run.vars["candidate_sent"] is True
    assert not await run.admits(
        "gobby-tasks",
        "close_task",
        {"task_id": task.id, "reason": "moot"},
    )


async def test_developer_landed_retest_unlocks_shell_without_proof_result(
    linked_developer: tuple[SeatRun, Task, str, str],
) -> None:
    run, task, sha, _ = linked_developer
    assert not await run.admits_native("Bash", {"command": "git status"})
    assert run.step == "submit"
    handoff = {"task_id": task.id, "reason": f"Handed off: reviewer LAND at {sha}; retest"}
    await run.call("gobby-tasks", "escalate_task", handoff)
    assert run.step == "claim"
    assert run.vars["_developer_handoff_tasks"] == [task.id]
    await run.call("gobby-tasks", "de_escalate_task", {"task_id": task.id, "reason": "Retest"})
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": f"#{task.seq_num}"}},
    )
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, {"result": {"id": task.id}})
    assert run.step == "implement"
    assert run.vars["assigned_task_id"] == task.id
    assert await run.admits_native("Bash", {"command": "git status"})


@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("use_ref", [False, True])
async def test_developer_closes_unsubmitted_no_commit_research(
    temp_db: HubDatabase, preview: bool, use_ref: bool
) -> None:
    run = SeatRun(temp_db, "developer")
    await run.load(*run.vars["required_skills"])
    task = run.task(owner=SESSION_ID)
    task_ref = f"#{task.seq_num}"
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": task_ref}},
    )
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, {"result": {"id": task.id}})
    assert run.step == "implement"
    assert not task.commits
    assert not run.vars["candidate_sent"]
    assert run.vars["_developer_handoff_tasks"] == []
    arguments = {
        "task_id": task_ref if use_ref else task.id,
        "preview": preview,
        "changes_summary": "Research complete; no code changes required.",
    }
    assert await run.admits("gobby-tasks", "close_task", arguments)
    foreign = run.task(owner=PEER_ID)
    assert not await run.admits("gobby-tasks", "close_task", {**arguments, "task_id": foreign.id})
    assert not await run.admits("gobby-tasks", "close_task", {"preview": preview})


@pytest.mark.parametrize("linked_commit", [False, True], ids=["supplied", "previously-linked"])
@pytest.mark.parametrize("use_ref", [False, True], ids=["uuid", "ref"])
async def test_developer_refuses_unsubmitted_commit_close(
    temp_db: HubDatabase, linked_commit: bool, use_ref: bool
) -> None:
    run = SeatRun(temp_db, "developer")
    await run.load(*run.vars["required_skills"])
    task = run.task(owner=SESSION_ID)
    if linked_commit:
        LocalTaskManager(temp_db).link_commit(task.id, SHA)
    task_ref = f"#{task.seq_num}"
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": task_ref}},
    )
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, {"result": {"id": task.id}})
    assert run.step == "implement"
    assert not run.vars["candidate_sent"]
    arguments: dict[str, Any] = {"task_id": task_ref if use_ref else task.id}
    if not linked_commit:
        arguments["commit_sha"] = SHA
    assert not await run.admits("gobby-tasks", "close_task", arguments)


def test_developer_submit_leaves_landing_and_close_to_the_dev_manager() -> None:
    source = " ".join((AGENTS_DIR / "developer.yaml").read_text().split())
    assert "Your Dev Manager lands the approved SHA and closes the task" in source
    assert "resend the unchanged CANDIDATE to your Dev Manager only" in source
    # Landing receipts, activation relays and landed-retest proofs are retired.
    for retired in ("EVENT=LANDED", "EVENT=PROOF_RESULT", "Release Manager GO", "Lane Manager"):
        assert retired not in source, retired


def _team_runbook(db: HubDatabase, task: Task, team_id: str) -> str:
    """A runbook session whose admission step recorded team ``team_id`` for ``task``."""
    runbook = PipelineDefinition(
        name="team-runbook", steps=[PipelineStep(id=ADMISSION_STEP, exec="true")]
    )
    executions = LocalPipelineExecutionManager(db, PROJECT_ID)
    execution = executions.create_execution(
        runbook.name,
        json.dumps({"task": task.id}),
        definition_json=runbook.model_dump_json(),
        project_id=PROJECT_ID,
    )
    step = executions.create_step_execution(execution.id, ADMISSION_STEP)
    team = {item.name: f"{team_id}-{item.name}" for item in fields(TeamBinding)}
    team.update(team_id=team_id, task_id=task.id, project_id=PROJECT_ID)
    executions.update_step_execution(step.id, output_json=json.dumps({"team": team}))
    session = SessionManager(db).register(
        external_id=f"pipeline-{execution.id}",
        machine_id=AUTHORITY_MACHINE_ID,
        source="pipeline",
        project_id=PROJECT_ID,
    )
    return session.id


@pytest.fixture
async def reviewer_authorities(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> tuple[SeatRun, Task, dict[str, str]]:
    patch_local_machine_id(monkeypatch, AUTHORITY_MACHINE_ID)
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    other_task = run.task()
    # The binding test reads the closing grant from the installed definition.
    AgentDefinitionManager(temp_db).create(
        "dev-manager",
        {
            "name": "dev-manager",
            "version": "1.0",
            "prompts": {"agent": "Manage the team."},
            "workflows": {"rule_selectors": {"include": []}},
            "task_step_writes": ["closing"],
        },
        project_id=PROJECT_ID,
    )
    teams = {
        "own": _team_runbook(temp_db, task, "team-own"),
        "other": _team_runbook(temp_db, other_task, "team-other"),
    }
    actors = {
        "author": OWNER_ID,
        "reviewer": SESSION_ID,
        "orchestrator": COORDINATOR_ID,
        "other_reviewer": PEER_ID,
    }
    # The own and unbound managers sit in the reviewer's team; only the own one has a
    # running run bound to the task. The other manager runs another task's team.
    for name, team in (("own_dm", "own"), ("other_dm", "other"), ("unbound_dm", "own")):
        actor_session = run.sessions.register(
            external_id=name,
            source="codex",
            project_id=PROJECT_ID,
            machine_id=AUTHORITY_MACHINE_ID,
            parent_session_id=teams[team],
        )
        actors[name] = actor_session.id
        SessionVariableManager(temp_db).set_variable(actor_session.id, "_agent_type", "dev-manager")
    temp_db.execute(
        "UPDATE sessions SET parent_session_id = %s WHERE id = %s", (teams["own"], SESSION_ID)
    )
    runs = LocalAgentRunManager(temp_db)
    for actor, role, bound in (
        (SESSION_ID, "code-reviewer", None),
        (actors["own_dm"], "dev-manager", task.id),
        (actors["other_dm"], "dev-manager", other_task.id),
    ):
        record = runs.create(
            parent_session_id=COORDINATOR_ID,
            provider="codex",
            prompt="Seat authority fixture",
            agent_name=role,
            child_session_id=actor,
            task_id=bound,
        )
        runs.start(record.id)
        temp_db.execute("UPDATE sessions SET agent_run_id = %s WHERE id = %s", (record.id, actor))
    for reviewer in (SESSION_ID, PEER_ID):
        SessionTaskManager(temp_db).link_task(reviewer, task.id, "reviewing")
    await run.intake(task)
    assert run.step == "review"
    return run, task, actors


@pytest.mark.parametrize(
    "actor",
    ["author", "reviewer", "orchestrator", "own_dm", "other_dm", "other_reviewer", "unbound_dm"],
)
async def test_reviewer_withdrawal_requires_own_unit_authority(
    reviewer_authorities: tuple[SeatRun, Task, dict[str, str]], actor: str
) -> None:
    run, task, actors = reviewer_authorities
    before, step = run.vars, run.step
    line = f"EVENT=CANDIDATE_WITHDRAWN TASK={task.id} SHA={SHA}"
    if actor == "reviewer":
        await run.send(line)
    else:
        await run.incoming(line, actors[actor])
    if actor in {"reviewer", "orchestrator", "own_dm"}:
        assert run.step == "await"
        assert run.vars["candidate_received"] is False
        assert "parked_candidates" not in run.vars
    else:
        assert run.vars == before
        assert run.step == step


@pytest.mark.parametrize("mismatch", ["task", "sha"])
@pytest.mark.parametrize("actor", ["reviewer", "orchestrator", "own_dm"])
async def test_reviewer_withdrawal_refuses_another_unit(
    reviewer_authorities: tuple[SeatRun, Task, dict[str, str]],
    mismatch: str,
    actor: str,
) -> None:
    run, task, actors = reviewer_authorities
    before, step = run.vars, run.step
    other = run.task()
    task_id = other.id if mismatch == "task" else task.id
    sha = REPLACEMENT_SHA if mismatch == "sha" else SHA
    line = f"EVENT=CANDIDATE_WITHDRAWN TASK={task_id} SHA={sha}"
    if actor == "reviewer":
        await run.send(line)
    else:
        await run.incoming(line, actors[actor])
    assert run.vars == before
    assert run.step == step


@pytest.mark.parametrize("name", ["log-monitor", "metrics-monitor"])
async def test_monitor_windows_complete_from_durable_reports(
    temp_db: HubDatabase, name: str
) -> None:
    run = SeatRun(temp_db, name)
    SessionVariableManager(run.db).set_variable(PEER_ID, "_agent_type", "log-monitor")
    # Monitors report to Triage; a report to the Orchestrator completes nothing.
    SessionVariableManager(run.db).set_variable(COORDINATOR_ID, "_agent_type", "orchestrator")
    await run.load(*run.vars["required_skills"])
    await run.send("Systems nominal | sample window", COORDINATOR_ID)
    assert run.step == "tick" and run.vars["tick_window"] is None
    SessionVariableManager(run.db).set_variable(COORDINATOR_ID, "_agent_type", "triage")
    for _ in range(2):
        await run.send("Systems nominal | sample window", COORDINATOR_ID)
        if name == "metrics-monitor":
            assert run.step == "report"
            await run.send("Systems nominal | a different window", PEER_ID)
            assert run.step == "report"
            assert run.vars["log_monitor_reported"] is False
            await run.send("Systems nominal | sample window", PEER_ID)
        assert run.step == "tick"
        assert run.vars["tick_done"] is False
        assert run.vars["tick_window"]


async def test_plan_adversary_consensus_withdrawal_reopens_review(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    assert run.step == "review"
    await run.send(f"EVENT=CONSENSUS SHA={SHA} NOTE=approved")
    assert run.step == "stamp"
    await run.send(f"EVENT=CONSENSUS_WITHDRAWN SHA={REPLACEMENT_SHA} NOTE=invalid")
    assert run.step == "stamp"
    await run.send(f"EVENT=CONSENSUS_WITHDRAWN SHA={SHA} NOTE=unresolved")
    assert run.step == "review"
    assert run.vars["consensus"] is False


async def test_plan_adversary_consensus_line_may_precede_more_lines(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    await run.send(f"EVENT=CONSENSUS SHA={SHA}\nNOTE=approved\nEvidence follows.")
    assert run.step == "stamp"
    assert run.vars["consensus_sha"] == SHA
    await run.send(f"EVENT=CONSENSUS_WITHDRAWN SHA={SHA}\nRe-review needed.")
    assert run.step == "review"
    assert run.vars["consensus"] is False


async def test_plan_adversary_unbound_stamp_can_withdraw(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    # Reproduce a legacy native flag write without a handler-owned commit binding.
    instance.current_step = "stamp"
    instance.variables["consensus"] = True
    instance.variables.pop("consensus_sha", None)
    run.instances.save(instance)

    await run.incoming(f"EVENT=CONSENSUS_WITHDRAWN SHA={SHA}")
    assert run.step == "stamp"
    await run.send("EVENT=CONSENSUS_WITHDRAWN SHA=invalid")
    assert run.step == "stamp"
    await run.send(f"EVENT=CONSENSUS_WITHDRAWN SHA={SHA} NOTE=re-review needed")
    assert run.step == "review"
    assert run.vars["consensus"] is False
    assert not await run.admits(*DERIVE)


@pytest.mark.parametrize("step", ["review", "stamp"])
@pytest.mark.parametrize("name,value", [("consensus", True), ("consensus_sha", SHA)])
async def test_plan_adversary_consensus_variables_are_handler_owned(
    temp_db: HubDatabase, step: str, name: str, value: object
) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    if step == "stamp":
        await run.send(f"EVENT=CONSENSUS SHA={SHA}")
    before = dict(run.vars)
    assert not await run.admits_native(
        "mcp__gobby__set_variable",
        {"name": name, "value": value, "session_id": SESSION_ID, "scope": "step"},
    )
    assert run.vars == before
    assert run.step == step


@pytest.mark.parametrize(
    "content",
    [
        "EVENT=CONSENSUS",
        "EVENT=CONSENSUS ROUND=1",
        f"Cannot send EVENT=CONSENSUS SHA={SHA}; no committed entry exists.",
        f"EVENT=CONSENSUS_EXTRA SHA={SHA}",
        "EVENT=CONSENSUS SHA=not-a-commit",
        "EVENT=CONSENSUS SHA=",
        "EVENT=CONSENSUS SHA=abcdef",
        f"EVENT=CONSENSUS SHA={SHA[:7]}",
        f"EVENT=CONSENSUS SHA={SHA[:39]}",
        f"EVENT=CONSENSUS SHA={SHA}a",
        f"EVENT=CONSENSUS\nSHA={SHA}",
    ],
)
async def test_plan_adversary_rejects_malformed_consensus(
    temp_db: HubDatabase, content: str
) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    await run.send(content)
    assert run.step == "review"
    assert run.vars["consensus"] is False
    assert not await run.admits(*DERIVE)


async def test_plan_adversary_failed_consensus_send_stays_in_review(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    content = f"EVENT=CONSENSUS SHA={SHA}"
    message = InterSessionMessageManager(temp_db).create_message(
        SESSION_ID, COORDINATOR_ID, content
    )
    await run.call(
        *SEND,
        {"target": "session", "target_id": COORDINATOR_ID, "content": content},
        {"success": False, "message_ids": [message.id]},
    )
    assert run.step == "review"
    assert run.vars["consensus"] is False
    assert not await run.admits(*DERIVE)


async def test_plan_adversary_old_stamp_snapshot_uses_current_withdrawal(
    temp_db: HubDatabase,
) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    await run.send(f"EVENT=CONSENSUS SHA={SHA}")
    assert run.step == "stamp"
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    definitions = AgentDefinitionManager(temp_db)
    definition = definitions.get_by_name("plan-adversary")
    assert definition is not None
    definitions.set_step_workflow_from_sync(
        definition.id, instance.snapshot.model_dump(mode="json")
    )
    old_stamp = instance.snapshot.get_step("stamp")
    assert old_stamp is not None
    old_stamp.on_mcp_success = []
    old_stamp.transitions = []
    run.instances.replace_for_session(instance)

    await run.send(f"EVENT=CONSENSUS_WITHDRAWN SHA={SHA}")

    assert run.step == "review"
    assert run.vars["consensus"] is False
    assert not await run.admits(*DERIVE)
    retained = run.instances.get_for_session(SESSION_ID)
    assert retained is not None
    retained_stamp = retained.snapshot.get_step("stamp")
    assert retained_stamp is not None
    assert retained_stamp.transitions == []


async def test_developer_claim_keeps_ref_when_task_card_is_offloaded(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "developer")
    await run.load(*run.vars["required_skills"])
    task = run.task(owner=SESSION_ID)
    ref = f"#{task.seq_num}"
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": ref}},
    )
    assert run.vars["assigned_task_ref"] == ref
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, {"result": {"id": task.id}})
    assert run.step == "implement"
    assert run.vars["assigned_task_ref"] == ref


async def test_developer_submit_unlinks_a_superseded_commit(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "developer")
    await run.load(*run.vars["required_skills"])
    task = run.task(owner=SESSION_ID)
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": f"#{task.seq_num}"}},
    )
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, {"result": {"id": task.id}})
    await run.call("gobby-tasks", "link_commit", {"task_id": task.id, "commit_sha": SHA})
    await run.call(
        "gobby-tasks", "link_commit", {"task_id": task.id, "commit_sha": REPLACEMENT_SHA}
    )
    await run.call("gobby-tasks", "unlink_commit", {"task_id": task.id, "commit_sha": SHA})
    assert run.step == "submit"
    assert run.vars["bounced"] is False
    assert run.vars["implementation_complete"] is True


@pytest.mark.parametrize("name", ["developer", "code-reviewer", "researcher", "consolidator"])
async def test_standing_seats_never_use_native_idle_writes(temp_db: HubDatabase, name: str) -> None:
    run = SeatRun(temp_db, name)
    await run.load(*run.vars["required_skills"])
    assert not await run.admits_native(
        "mcp__gobby__set_variable",
        {
            "name": "seat_idle",
            "value": True,
            "session_id": SESSION_ID,
        },
    )


@pytest.mark.parametrize(
    "content, failure",
    [
        ("EVENT=SEAT_READY NOTE=idle", ""),
        ("Systems nominal | ready", ""),
        ("EVENT=SEAT_READY NOTE=busy", ""),
        ("EVENT=SEAT_READY NOTE=idle", "direct"),
        ("EVENT=SEAT_READY NOTE=idle", "content"),
        ("EVENT=SEAT_READY NOTE=idle", "structured"),
        ("EVENT=SEAT_READY NOTE=idle", "transport"),
        ("EVENT=SEAT_READY NOTE=idle", "identity"),
    ],
)
async def test_idle_is_declared_only_by_readiness_rule(
    temp_db: HubDatabase, content: str, failure: str
) -> None:
    run = SeatRun(temp_db, "developer")
    await run.load(*run.vars["required_skills"])
    path = AGENTS_DIR.parent / "rules/stop-gates/declare-seat-idle-on-readiness.yaml"
    document = yaml.safe_load(path.read_text())
    rule = document["rules"]["declare-seat-idle-on-readiness"]
    body = RuleDefinitionBody.model_validate(
        {
            "event": rule["event"],
            "when": rule["when"],
            "effects": [rule["effect"]],
        }
    )
    run.engine.rule_manager.create(
        name="declare-seat-idle-on-readiness",
        definition_json=body.model_dump_json(),
        enabled=True,
        priority=6,
        tags=document["tags"],
    )
    run.session_vars.update({"current_step_idle": True, "task_claimed": False})
    message = InterSessionMessageManager(temp_db).create_message(
        SESSION_ID, COORDINATOR_ID, content
    )
    output: dict[str, Any] = {
        "success": failure not in {"direct", "content", "structured"},
        "message_ids": [message.id],
    }
    if failure == "content":
        output = {"content": [{"type": "text", "text": json.dumps(output)}]}
    elif failure == "structured":
        output = {"structuredContent": output}
    event = run._event(
        HookEventType.AFTER_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": SEND[0],
                "tool_name": SEND[1],
                "arguments": {"target": "session", "target_id": COORDINATOR_ID, "content": content},
            },
            "tool_output": output,
        },
    )
    if failure == "transport":
        event.metadata["is_failure"] = True
    if failure != "identity":
        event.metadata["_platform_session_id"] = SESSION_ID
    await run._evaluate(event)
    assert bool(run.session_vars.get("seat_idle")) == (
        content == "EVENT=SEAT_READY NOTE=idle" and not failure
    )


def _reviewer_stop_handler(run: SeatRun) -> WorkflowHookHandler:
    """Install the bundled gates and let hooks project the real step context."""
    for name in ("declare-seat-idle-on-readiness", "require-step-completion"):
        path = AGENTS_DIR.parent / f"rules/stop-gates/{name}.yaml"
        document = yaml.safe_load(path.read_text())
        rule = document["rules"][name]
        body = RuleDefinitionBody.model_validate(
            {
                "event": rule["event"],
                "when": rule["when"],
                "effects": rule.get("effects", [rule.get("effect")]),
            }
        )
        run.engine.rule_manager.create(
            name=name,
            definition_json=body.model_dump_json(),
            enabled=True,
            priority=rule.get("priority", 50),
            tags=document["tags"],
        )
    SessionVariableManager(run.db).merge_variables(
        SESSION_ID, {**run.session_vars, "is_spawned_agent": True}
    )
    return WorkflowHookHandler(
        rule_engine=run.engine,
        session_manager=run.sessions,
        task_manager=LocalTaskManager(run.db),
        session_task_manager=SessionTaskManager(run.db),
    )


def _reviewer_stop_event(cwd: Path) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.STOP,
        session_id="ext-seat",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        cwd=str(cwd),
        data={},
        metadata={"_platform_session_id": SESSION_ID},
    )


@pytest.mark.parametrize(
    "content, ready",
    [
        ("EVENT=SEAT_READY NOTE=idle", True),
        ("LANE=3 EVENT=SEAT_READY NOTE=idle", True),
        ("LANE= EVENT=SEAT_READY NOTE=idle", True),
        ("LANE=3 EVENT=SEAT_READY RUN=gobby#16009 NOTE=idle", True),
        ("EVENT=SEAT_READY RUN=gobby#16009 NOTE=idle", True),
        ("LANE=3 EVENT=OTHER NOTE=idle", False),
        ("LANE=3 EVENT=SEAT_READY prose NOTE=idle", False),
        ("LANE=3 EVENT=SEAT_READY NOTE=busy", False),
        ("LANE=3 EVENT=SEAT_READY OTHER=value NOTE=idle", False),
        ("LANE=3 EVENT=SEAT_READY RUN=one RUN=two NOTE=idle", False),
        ("LANE=3 EVENT=SEAT_READY NOTE=idle extra", False),
    ],
)
@pytest.mark.parametrize("envelope", ["direct", "content", "structured"])
@pytest.mark.parametrize("name", ["developer", "code-reviewer"])
async def test_reviewer_call_tool_readiness_allows_stop_with_external_session_id(
    temp_db: HubDatabase, tmp_path: Path, envelope: str, name: str, content: str, ready: bool
) -> None:
    run = SeatRun(temp_db, name)
    await run.load(*run.vars["required_skills"])
    idle_step = "claim" if name == "developer" else "await"
    assert run.step == idle_step
    handler = _reviewer_stop_handler(run)
    blocked = await handler.evaluate_async(_reviewer_stop_event(tmp_path))
    assert blocked.decision == "block" and "require-step-completion" in (blocked.reason or "")

    message = InterSessionMessageManager(temp_db).create_message(
        SESSION_ID, COORDINATOR_ID, content
    )
    wire = _process_tool_proxy_result(
        {"target": "session", "message_ids": [message.id], "delivery_status": "sent"},
        *SEND,
    )
    output: dict[str, Any] = wire
    if envelope == "content":
        output = {"isError": False, "content": [{"type": "text", "text": json.dumps(wire)}]}
    elif envelope == "structured":
        output = {"isError": False, "structuredContent": wire}
    completion = ClaudeCodeAdapter().translate_to_hook_event(
        {
            "hook_type": "post-tool-use",
            "input_data": {
                "session_id": "ext-seat",
                "cwd": str(tmp_path),
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": SEND[0],
                    "tool_name": SEND[1],
                    "arguments": {
                        "target": "session",
                        "target_id": COORDINATOR_ID,
                        "content": content,
                    },
                },
                "tool_response": output,
            },
        }
    )
    completion.metadata["_platform_session_id"] = SESSION_ID
    await handler.evaluate_async(completion)
    stopped = await handler.evaluate_async(_reviewer_stop_event(tmp_path))
    assert stopped.decision == ("allow" if ready else "block"), stopped.reason
    assert bool(SessionVariableManager(temp_db).get_variables(SESSION_ID).get("seat_idle")) is ready
    assert run.step == idle_step


@pytest.mark.parametrize("step", ["await", "review"])
async def test_reviewer_registered_coordination_wait_allows_stop_with_external_session_id(
    temp_db: HubDatabase, tmp_path: Path, step: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    if step == "review":
        await run.intake(run.task())
    assert run.step == step
    handler = _reviewer_stop_handler(run)
    blocked = await handler.evaluate_async(_reviewer_stop_event(tmp_path))
    assert blocked.decision == "block" and "require-step-completion" in (blocked.reason or "")
    wait = CoordinationWaitManager(temp_db).register(SESSION_ID, COORDINATOR_ID, reply=True)
    assert wait["outcome"] == "waiting"
    stopped = await handler.evaluate_async(_reviewer_stop_event(tmp_path))
    assert stopped.decision == "allow", stopped.reason
    assert not SessionVariableManager(temp_db).get_variables(SESSION_ID).get("seat_idle")
    assert run.step == step


@pytest.mark.parametrize(
    "name, step",
    [
        ("developer", "claim"),
        ("developer", "implement"),
        ("developer", "submit"),
        ("code-reviewer", "await"),
        ("code-reviewer", "review"),
        ("code-reviewer", "verdict"),
        ("plan-adversary", "review"),
    ],
)
@pytest.mark.parametrize("wait_state", ["active", "replied", "cancelled"])
async def test_standing_seat_wait_through_call_tool_controls_stop(
    temp_db: HubDatabase, tmp_path: Path, name: str, step: str, wait_state: str
) -> None:
    run = SeatRun(temp_db, name)
    await run.load(*run.vars["required_skills"])
    if name == "developer" and step != "claim":
        task = run.task(owner=SESSION_ID, skills=["rust"])
        ref = f"#{task.seq_num}"
        await run.call(
            "gobby-tasks",
            "claim_task",
            {"task_id": task.id},
            {"result": {"task_id": task.id, "ref": ref}},
        )
        await run.call(
            "gobby-tasks", "get_task", {"task_id": task.id}, {"result": {"id": task.id, "ref": ref}}
        )
        await run.load("rust")
        if step == "submit":
            await run.call("gobby-tasks", "link_commit", {"task_id": task.id, "commit_sha": SHA})
    elif name == "code-reviewer" and step == "review":
        await run.intake(run.task())
    elif step == "verdict":
        # A recovered legacy snapshot can retain verdict without a bound candidate.
        instance = run.instances.get_for_session(SESSION_ID)
        assert instance is not None
        instance.current_step = "verdict"
        run.instances.save(instance)
    assert run.step == step
    handler = _reviewer_stop_handler(run)
    blocked = await handler.evaluate_async(_reviewer_stop_event(tmp_path))
    assert blocked.decision == "block" and "require-step-completion" in (blocked.reason or "")
    arguments = {"owner_session": COORDINATOR_ID, "reply": True}
    assert await run.admits(*WAIT, arguments)
    registry = create_agents_registry(
        AgentRunner(temp_db, run.sessions),
        session_manager=run.sessions,
        db=temp_db,
        completion_registry=CompletionEventRegistry(),
    )
    with session_context_for_test(SESSION_ID):
        wait = await registry.call(WAIT[1], arguments)
    assert wait["outcome"] == "waiting"
    wire = _process_tool_proxy_result(wait, *WAIT)
    completion = ClaudeCodeAdapter().translate_to_hook_event(
        {
            "hook_type": "post-tool-use",
            "input_data": {
                "session_id": "ext-seat",
                "cwd": str(tmp_path),
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": WAIT[0],
                    "tool_name": WAIT[1],
                    "arguments": arguments,
                },
                "tool_response": wire,
            },
        }
    )
    completion.metadata["_platform_session_id"] = SESSION_ID
    await handler.evaluate_async(completion)
    if wait_state == "replied":
        InterSessionMessageManager(temp_db).create_message(COORDINATOR_ID, SESSION_ID, "Still held")
    elif wait_state == "cancelled":
        CoordinationWaitManager(temp_db).cancel(str(wait["wait_id"]), SESSION_ID)
    assert CoordinationWaitManager(temp_db).has_active_wait(SESSION_ID) is (wait_state == "active")
    stopped = await handler.evaluate_async(_reviewer_stop_event(tmp_path))
    assert stopped.decision == ("allow" if wait_state == "active" else "block"), stopped.reason
    assert not SessionVariableManager(temp_db).get_variables(SESSION_ID).get("seat_idle")


async def test_candidate_latch_survives_native_reset_and_blocks_already_implemented_close(
    linked_developer: tuple[SeatRun, Task, str, str],
) -> None:
    run, task, sha, _ = linked_developer
    assert run.vars["candidate_sent"] is True
    assert not await run.admits_native(
        "mcp__gobby__set_variable",
        {
            "name": "candidate_sent",
            "value": False,
            "scope": "step",
            "session_id": SESSION_ID,
        },
    )
    assert not await run.admits(
        "gobby-tasks",
        "close_task",
        {
            "task_id": task.id,
            "reason": "moot",
        },
    )
    assert run.vars["candidate_sent"] is True
    await run.incoming(
        f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={sha} VERDICT=BOUNCE",
        PEER_ID,
    )
    assert run.step == "implement"
    assert not await run.admits(
        "gobby-tasks",
        "close_task",
        {
            "task_id": task.id,
            "reason": "moot",
        },
    )
    assert run.vars["candidate_sent"] is True


@pytest.mark.parametrize("alias", ["hash", "number", "uuid"])
async def test_task_reads_and_replacement_accept_aliases(temp_db: HubDatabase, alias: str) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    ref = {"hash": f"#{task.seq_num}", "number": str(task.seq_num), "uuid": task.id}[alias]
    await run.incoming(f"EVENT=CANDIDATE TASK={ref} RUN=run WT=tree COMMIT={REPLACEMENT_SHA}")
    assert (run.vars["candidate_task"], run.vars["candidate_sha"]) == (task.id, REPLACEMENT_SHA)
    assert "parked_candidates" not in run.vars
    run.record_approval(task, REPLACEMENT_SHA)
    await run.call("gobby-tasks", "get_task", {"task_id": ref})
    assert run.step == "verdict"


@pytest.mark.parametrize("envelope", ["direct", "content", "structured"])
async def test_developer_rejected_link_commit_stays_in_implement(
    temp_db: HubDatabase, envelope: str
) -> None:
    run = SeatRun(temp_db, "developer")
    await run.load(*run.vars["required_skills"])
    task = run.task(owner=SESSION_ID)
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": f"#{task.seq_num}"}},
    )
    await run.call("gobby-tasks", "get_task", {"task_id": task.id})
    assert run.step == "implement"

    def delivered(result: dict[str, Any]) -> dict[str, Any]:
        wire = _process_tool_proxy_result(result, "gobby-tasks", "link_commit")
        if envelope == "content":
            return {"isError": False, "content": [{"type": "text", "text": json.dumps(wire)}]}
        if envelope == "structured":
            return {"isError": False, "structuredContent": wire}
        return wire

    event: dict[str, Any] = {
        "hook_type": "post-tool-use",
        "input_data": {
            "session_id": SESSION_ID,
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "link_commit",
                "arguments": {"task_id": task.id, "commit_sha": SHA},
            },
            "tool_response": delivered({"error": "Invalid or unresolved commit SHA"}),
        },
    }
    adapter = ClaudeCodeAdapter()
    await run._evaluate(adapter.translate_to_hook_event(event))
    assert (run.step, run.vars["implementation_complete"]) == ("implement", False)
    event["input_data"]["tool_response"] = delivered(
        {"task_id": task.id, "commits": [SHA], "error": None}
    )
    await run._evaluate(adapter.translate_to_hook_event(event))
    assert (run.step, run.vars["implementation_complete"]) == ("submit", True)


@pytest.mark.parametrize("envelope", ["direct", "content", "structured"])
async def test_failed_verdict_send_keeps_unit_even_with_stored_message(
    temp_db: HubDatabase, envelope: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    await run.approve(task)
    line = f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={SHA} VERDICT=BOUNCE"
    message = InterSessionMessageManager(run.db).create_message(SESSION_ID, COORDINATOR_ID, line)
    failure: dict[str, Any] = {
        "success": False,
        "error": "DAEMON_UNAVAILABLE",
        "message_ids": [message.id],
    }
    output = failure
    if envelope == "content":
        output = {"isError": False, "content": [{"type": "text", "text": json.dumps(failure)}]}
    elif envelope == "structured":
        output = {"isError": False, "structuredContent": failure}
    event: dict[str, Any] = {
        "hook_type": "post-tool-use",
        "input_data": {
            "session_id": SESSION_ID,
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": SEND[0],
                "tool_name": SEND[1],
                "arguments": {"content": line},
            },
            "tool_response": output,
        },
    }
    adapter = ClaudeCodeAdapter()
    await run._evaluate(adapter.translate_to_hook_event(event))
    assert run.step == "verdict"
    assert run.vars["candidate_task"] == task.id
    event["input_data"]["tool_response"] = {"success": True, "message_ids": [message.id]}
    await run._evaluate(adapter.translate_to_hook_event(event))
    assert run.step == "await"


@pytest.mark.parametrize(
    "format_name, reason",
    [
        ("canonical", None),
        ("extras", None),
        ("reordered", None),
        ("comma-run", None),
        ("upper-commit", None),
        ("free-text", None),
        ("old-extras", "expected KEY=value token: passed"),
        ("old-reordered", None),
        ("old-canonical", None),
        ("missing-task", "missing TASK"),
        ("missing-run", "missing RUN"),
        ("missing-wt", "missing WT"),
        ("missing-commit", "missing COMMIT"),
        ("short-commit", "COMMIT must be 40 hex characters"),
        ("bad-commit", "COMMIT must be 40 hex characters"),
        ("duplicate-event", "duplicate EVENT"),
        ("duplicate-task", "duplicate TASK"),
        ("duplicate-run", "duplicate RUN"),
        ("duplicate-wt", "duplicate WT"),
        ("duplicate-commit", "duplicate COMMIT"),
    ],
)
async def test_candidate_read_reports_binding_or_rejection(
    temp_db: HubDatabase, format_name: str, reason: str | None
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    envelope = f"EVENT=CANDIDATE TASK={task.id} RUN=run WT=tree COMMIT={SHA}"
    lines = {
        "canonical": f"LANE=1 EVENT=CANDIDATE TASK={task.id} TASK_TITLE=title RUN=run WT=tree COMMIT={SHA} NOTE=ready",
        # Reformatted e0e8c549: TESTS is one token; its prose belongs in NOTE.
        "extras": f"LANE=7 EVENT=CANDIDATE TASK={task.id} TASK_TITLE=Seat proofs RUN=run WT=tree COMMIT={SHA} BASE=base TESTS=481 TREE=tree NOTE=481 passed",
        "reordered": f"EVENT=CANDIDATE TASK={task.id} WT=tree COMMIT={SHA} RUN=run NOTE=ready",
        "comma-run": f"EVENT=CANDIDATE TASK={task.id} RUN=red,green WT=relative COMMIT={SHA}",
        "upper-commit": envelope.replace(SHA, SHA.upper()),
        "free-text": f"{envelope} NOTE=words RUN=other WT=other COMMIT={REPLACEMENT_SHA}",
        # Exact receipt grammar from e0e8c549 and 24bca8f9 respectively.
        "old-extras": f"LANE=7 EVENT=CANDIDATE TASK={task.id} TASK_TITLE=Seat proofs RUN=run WT=tree COMMIT={SHA} BASE=base TESTS=481 passed NOTE=ready",
        "old-reordered": f"LANE=2 EVENT=CANDIDATE TASK={task.id} TASK_TITLE=Pane detectors RUN=RED-28AB6-EXIT100-3FAIL,GREEN-F9AE-3PASS,FMT-F9AE-PASS,CLIPPY-F9AE-0ERR-1WARN-OUTSIDE COMMIT={SHA} WT=lane-2-front-door-6 NOTE=ready",
        "old-canonical": f"EVENT=CANDIDATE TASK={task.id} TASK_TITLE=title RUN=run WT=tree COMMIT={SHA}",
        "missing-task": f"EVENT=CANDIDATE RUN=run WT=tree COMMIT={SHA}",
        "missing-run": f"EVENT=CANDIDATE TASK={task.id} WT=tree COMMIT={SHA}",
        "missing-wt": f"EVENT=CANDIDATE TASK={task.id} RUN=run COMMIT={SHA}",
        "missing-commit": f"EVENT=CANDIDATE TASK={task.id} RUN=run WT=tree NOTE=COMMIT={SHA}",
        "short-commit": envelope.replace(SHA, SHA[:10]),
        "bad-commit": envelope.replace(SHA, "z" * 40),
        "duplicate-event": f"{envelope} EVENT=CANDIDATE",
        "duplicate-task": f"{envelope} TASK={task.id}",
        "duplicate-run": f"{envelope} RUN=other",
        "duplicate-wt": f"{envelope} WT=other",
        "duplicate-commit": f"{envelope} COMMIT={SHA}",
    }
    message = InterSessionMessageManager(temp_db).create_message(
        OWNER_ID, SESSION_ID, lines[format_name]
    )
    registry = InternalToolRegistry(name="gobby-agents", description="Candidate intake")
    add_messaging_tools(registry, InterSessionMessageManager(temp_db), run.sessions, temp_db)
    with session_context_for_test(SESSION_ID):
        result = await registry.call("get_inter_session_message", {"message_id": message.id})
    assert result["success"] is True
    assert result["message"]["content"] == lines[format_name]
    if reason is None:
        assert "candidate_binding_error" not in result
    else:
        assert result.get("candidate_binding_error") == _CANDIDATE_GRAMMAR
    await run.call("gobby-agents", "get_inter_session_message", {"message_id": message.id}, result)
    assert run.step == ("review" if reason is None else "await")
    assert run.vars["candidate_sha"] == (SHA if reason is None else None)


@pytest.mark.parametrize("rejection", ["sender", "active-task", "stale", "consumed"])
async def test_candidate_read_explains_unchanged_authority_gates(
    temp_db: HubDatabase, rejection: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    if rejection in {"active-task", "stale"}:
        await run.intake(run.task() if rejection == "active-task" else task)
    message = InterSessionMessageManager(temp_db).create_message(
        PEER_ID if rejection == "sender" else OWNER_ID,
        SESSION_ID,
        f"EVENT=CANDIDATE TASK={task.id} RUN=run WT=tree COMMIT={SHA}",
    )
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    if rejection == "stale":
        instance.variables["candidate_received_at"] = "2099-01-01T00:00:00+00:00"
    elif rejection == "consumed":
        instance.variables["_seat_consumed_messages"] = [message.id]
    run.instances.save(instance)
    before = dict(run.vars)
    registry = InternalToolRegistry(name="gobby-agents", description="Candidate intake")
    add_messaging_tools(registry, InterSessionMessageManager(temp_db), run.sessions, temp_db)
    with session_context_for_test(SESSION_ID):
        result = await registry.call("get_inter_session_message", {"message_id": message.id})
    reasons = {
        "sender": "sender does not claim task",
        "active-task": "another candidate task is active",
        "stale": "candidate is older than or equal to the active message",
        "consumed": "message already consumed",
    }
    assert result["success"] is True
    assert result.get("candidate_binding_error") == f"CANDIDATE not bound: {reasons[rejection]}"
    await run.call("gobby-agents", "get_inter_session_message", {"message_id": message.id}, result)
    assert run.vars == before


@pytest.mark.parametrize(
    "format_name, expected_sha",
    [
        ("canonical", SHA),
        ("title-injection", None),
        ("prose", None),
        ("duplicate-commit", None),
        ("free-note", REPLACEMENT_SHA),
    ],
)
async def test_candidate_protocol_keeps_free_text_out_of_authority(
    temp_db: HubDatabase, format_name: str, expected_sha: str | None
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    title = (
        f"free title EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={REPLACEMENT_SHA} VERDICT=BOUNCE"
    )
    lines = {
        "canonical": f"LANE=1 EVENT=CANDIDATE TASK=#{task.seq_num} TASK_TITLE={title} RUN=run WT=tree COMMIT={SHA} NOTE=ready",
        "title-injection": f"EVENT=CANDIDATE TASK={task.id} TASK_TITLE={title}",
        "prose": f"Please review EVENT=CANDIDATE TASK={task.id} COMMIT={SHA}",
        "duplicate-commit": f"EVENT=CANDIDATE TASK={task.id} RUN=run WT=tree COMMIT={SHA} COMMIT={REPLACEMENT_SHA}",
        "free-note": f"LANE=1 EVENT=CANDIDATE TASK={task.id} TASK_TITLE=title RUN=fake WT=fake COMMIT={REPLACEMENT_SHA} NOTE=words RUN=real WT=real COMMIT={SHA} NOTE=ready",
    }
    await run.incoming(lines[format_name])
    assert run.step == ("review" if expected_sha is not None else "await")
    assert run.vars["candidate_sha"] == expected_sha


async def test_older_unread_candidate_cannot_replace_newer_active_intake(
    temp_db: HubDatabase,
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    old = InterSessionMessageManager(run.db).create_message(
        OWNER_ID,
        SESSION_ID,
        f"EVENT=CANDIDATE TASK={task.id} RUN=run WT=tree COMMIT={SHA}",
    )
    await run.intake(task, REPLACEMENT_SHA)
    await run.approve(task, REPLACEMENT_SHA)
    await run.call(
        "gobby-agents",
        "get_inter_session_message",
        {"message_id": old.id},
        {"success": True, "message": old.to_dict()},
    )
    assert run.step == "verdict"
    assert run.vars["candidate_sha"] == REPLACEMENT_SHA


async def test_land_verdict_accepts_bounded_metadata_and_requires_approval(
    temp_db: HubDatabase,
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    line = (
        f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={SHA} VERDICT=LAND "
        "TASK_TITLE=Bounded title NOTE=finished"
    )
    await run.send(line)
    assert run.step == "review"
    await run.approve(task)
    await run.send(line)
    assert run.step == "await"
    assert run.vars["candidate_task"] is None


async def test_withdrawal_repairs_unbound_stale_session_candidate(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_local_machine_id(monkeypatch, "21000000-0000-4000-8000-000000000001")
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    await run.approve(task)
    stale = dict(run.vars)
    # Reproduce a copy left behind by an older daemon: the instance is unbound,
    # the session copy still names the candidate.
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    instance.variables.update(_CANDIDATE_DEFAULTS)
    instance.current_step = "await"
    run.instances.save(instance)
    session_vars = SessionVariableManager(temp_db)
    session_vars.merge_variables(SESSION_ID, stale)
    run.session_vars.update(stale)
    line = f"EVENT=CANDIDATE_WITHDRAWN TASK={task.id} SHA={SHA}"
    await run.incoming(line, sender=COORDINATOR_ID)
    assert {key: run.session_vars[key] for key in _CANDIDATE_DEFAULTS} == _CANDIDATE_DEFAULTS
    # Persist as WorkflowHookHandler does and verify an idempotent second delivery.
    session_vars.merge_variables(SESSION_ID, run.session_vars)
    await run.incoming(line, sender=COORDINATOR_ID)
    stored = session_vars.get_variables(SESSION_ID)
    assert {key: stored[key] for key in _CANDIDATE_DEFAULTS} == _CANDIDATE_DEFAULTS
    replacement = run.task()
    await run.intake(replacement, REPLACEMENT_SHA)
    assert run.step == "review"
    assert run.vars["candidate_task"] == replacement.id


async def test_candidate_intake_survives_audit_failure(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit sink unavailable")

    monkeypatch.setattr(WorkflowAuditManager, "log", unavailable)
    await run.intake(task)
    assert run.step == "review"
    assert run.vars["candidate_task"] == task.id
    assert run.vars["candidate_sha"] == SHA
    assert "audit sink unavailable" in caplog.text


@pytest.mark.parametrize("target", ["project", "global"])
async def test_unknown_candidate_event_fanout_is_unchanged(
    temp_db: HubDatabase, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_local_machine_id(monkeypatch, "21000000-0000-4000-8000-000000000001")
    run = SeatRun(temp_db, "code-reviewer")
    messages = InterSessionMessageManager(temp_db)
    registry = InternalToolRegistry(name="gobby-agents", description="Candidate fanout")
    add_messaging_tools(registry, messages, run.sessions, temp_db)
    content = f"EVENT=CANDIDATE_UPDATE TASK=#7 COMMIT={SHA}"

    with session_context_for_test(OWNER_ID):
        result = await registry.call(
            "send_message", {"target": target, "content": content, "wake": False}
        )

    assert result["success"] is True
    assert "candidate_binding_error" not in result
    assert [message.content for message in messages.get_messages(SESSION_ID)] == [content]


@pytest.mark.parametrize("target", ["session", "agent"])
async def test_candidate_send_refuses_unknown_task_before_storing(
    temp_db: HubDatabase, target: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    target_id = SESSION_ID
    if target == "agent":
        target_id = (
            LocalAgentRunManager(temp_db)
            .create(
                parent_session_id=COORDINATOR_ID,
                child_session_id=SESSION_ID,
                provider="codex",
                prompt="Review",
            )
            .id
        )
    messages = InterSessionMessageManager(temp_db)
    registry = InternalToolRegistry(name="gobby-agents", description="Candidate send")
    add_messaging_tools(registry, messages, run.sessions, temp_db)

    with session_context_for_test(OWNER_ID):
        result = await registry.call(
            "send_message",
            {
                "target": target,
                "target_id": target_id,
                "content": f"EVENT=CANDIDATE TASK=#999999 RUN=run WT=tree COMMIT={SHA}",
                "wake": False,
            },
        )

    assert result.get("candidate_binding_error") == "unknown TASK"
    assert result["error"] == "unknown TASK"
    assert result["error_code"] == "candidate_binding_error"
    assert result["success"] is False
    assert messages.get_messages(SESSION_ID) == []


@pytest.mark.parametrize("busy", [False, True])
async def test_candidate_send_stores_and_queues_for_reviewer(
    temp_db: HubDatabase, busy: bool
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    active = run.task()
    if busy:
        await run.intake(active)
    queued = run.task()
    messages = InterSessionMessageManager(temp_db)
    registry = InternalToolRegistry(name="gobby-agents", description="Candidate send")
    add_messaging_tools(registry, messages, run.sessions, temp_db)
    content = f"EVENT=CANDIDATE TASK={queued.id} RUN=run WT=tree COMMIT={SHA}"

    with session_context_for_test(OWNER_ID):
        result = await registry.call(
            "send_message",
            {"target": "session", "target_id": SESSION_ID, "content": content, "wake": False},
        )

    assert result["success"] is True
    assert "candidate_binding_error" not in result
    message = messages.get_message(result["message_ids"][0])
    assert message is not None
    assert message.content == content
    if busy:
        assert run.vars["candidate_task"] == active.id
    else:
        with session_context_for_test(SESSION_ID):
            intake = await registry.call("get_inter_session_message", {"message_id": message.id})
        await run.call(
            "gobby-agents", "get_inter_session_message", {"message_id": message.id}, intake
        )
        assert run.step == "review"
        assert run.vars["candidate_task"] == queued.id


@pytest.mark.parametrize("prefix", ["", "LANE=1 "])
@pytest.mark.parametrize(
    "event, sha",
    [
        ("CANDIDATE", SHA),
        ("EVENT=CANDIDATE_WITHDRAWN", "bad"),
        ("EVENT=CANDIDATE_WITHDRAWN", "a" * 10),
        ("EVENT=CANDIDATE_WITHDRAWN", "z" * 40),
        ("EVENT=CANDIDATE_RESUMED", "a" * 10),
        ("EVENT=CANDIDATE_PARKED", "z" * 40),
    ],
)
async def test_candidate_malformed_protocol_reports_grammar(
    temp_db: HubDatabase, prefix: str, event: str, sha: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    suffix = f"RUN=r WT=t COMMIT={sha}" if event == "CANDIDATE" else f"SHA={sha}"
    message = InterSessionMessageManager(temp_db).create_message(
        OWNER_ID, SESSION_ID, f"{prefix}{event} TASK={task.id} {suffix}"
    )
    registry = InternalToolRegistry(name="gobby-agents", description="Candidate intake")
    add_messaging_tools(registry, InterSessionMessageManager(temp_db), run.sessions, temp_db)
    with session_context_for_test(SESSION_ID):
        result = await registry.call("get_inter_session_message", {"message_id": message.id})
    from gobby.workflows.engine.seat_messages import _COORDINATION_GRAMMAR

    expected = (
        _CANDIDATE_GRAMMAR
        if event == "CANDIDATE"
        else "CANDIDATE not bound: " + _COORDINATION_GRAMMAR[event.removeprefix("EVENT=")]
    )
    assert result.get("candidate_binding_error") == expected
    await run.call("gobby-agents", "get_inter_session_message", {"message_id": message.id}, result)
    assert run.step == "await"
    assert run.vars["candidate_task"] is None


async def test_seat_consumed_messages_keeps_last_64(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    message_ids = []
    for _ in range(70):
        message_ids.append(
            await run.incoming(f"EVENT=CANDIDATE TASK={task.id} RUN=r WT=t COMMIT={SHA}")
        )
    assert run.vars["_seat_consumed_messages"] == message_ids[-64:]
    assert run.step == "review"


@pytest.mark.parametrize(
    "source",
    [
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.DROID,
        SessionSource.GROK,
        SessionSource.AGY,
    ],
)
@pytest.mark.parametrize("omit_input", [False, True])
@pytest.mark.parametrize("explicit_ref", [False, True])
async def test_spawned_proxy_and_provider_complete_step_once(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    source: SessionSource,
    omit_input: bool,
    explicit_ref: bool,
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    # A counter detects duplicate handlers even when seat-message dedup masks them.
    definition = AgentDefinitionManager(temp_db).get_by_name("code-reviewer")
    assert definition is not None
    body = instance.snapshot.model_dump()
    body["variables"]["completion_count"] = 0
    body["steps"][1]["on_mcp_success"].append(
        {
            "server": "gobby-agents",
            "tool": "wait_for_coordination",
            "action": "set_variable",
            "variable": "completion_count",
            "value": "(vars.completion_count or 0) + 1",
        }
    )
    AgentDefinitionManager(temp_db).upsert_with_steps(
        "code-reviewer", definition.definition_json, body
    )
    instance.variables["completion_count"] = 0
    run.instances.save(instance)
    SessionVariableManager(temp_db).merge_variables(SESSION_ID, run.session_vars)
    handler = WorkflowHookHandler(
        rule_engine=run.engine, evaluation_runtime=WorkflowEvaluationRuntime(max_workers=1)
    )
    hook_manager = MagicMock()
    hook_manager._workflow_handler = handler
    hook_manager.event_handlers = None
    hook_manager._session_manager = run.sessions
    internal = MagicMock()
    internal.is_internal.return_value = True
    internal.get_registry.return_value.call = AsyncMock(return_value={"ready": True})
    internal.get_registry.return_value.get_schema.return_value = {
        "name": "wait_for_coordination",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "timeout": {"type": "integer"}},
            "required": ["session_id"],
        },
    }
    mcp_manager = MagicMock()
    mcp_manager.session_manager = run.sessions
    mcp_manager.project_id = PROJECT_ID
    proxy = ToolProxyService(
        mcp_manager, internal_manager=internal, hook_manager_resolver=lambda: hook_manager
    )
    metadata = {"_platform_session_id": SESSION_ID}
    monkeypatch.setattr(
        proxy,
        "_resolve_tool_event_context",
        lambda _: (
            hook_manager,
            run.sessions,
            SimpleNamespace(agent_run_id="spawned-seat"),
            source,
            dict(metadata),
            None,
            PROJECT_ID,
        ),
    )
    monkeypatch.setattr(proxy, "_get_effective_session_id", lambda _: SESSION_ID)
    temp_db.execute("UPDATE sessions SET seq_num = %s WHERE id = %s", (123, SESSION_ID))
    session = run.sessions.get(SESSION_ID)
    assert session is not None
    arguments: dict[str, Any] = {"timeout": 1}
    if explicit_ref:
        arguments["session_id"] = f"#{session.seq_num}"
    data = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_use_id": "call-1",
        "tool_input": {
            "server_name": "gobby-agents",
            "tool_name": "wait_for_coordination",
            "args": json.dumps(arguments),
            "session_id": SESSION_ID,
            "intent": "test completion correlation",
        },
    }
    try:
        # The second call has identical arguments but its own completion allowance.
        for count in (1, 2):
            data["tool_use_id"] = f"call-{count}"
            before = run._event(HookEventType.BEFORE_TOOL, dict(data))
            before.source = source
            before.metadata.update(metadata)
            assert (await asyncio.to_thread(handler.evaluate, before)).decision == "allow"
            with session_context_for_test(SESSION_ID):
                result = await proxy.call_tool(
                    "gobby-agents",
                    "wait_for_coordination",
                    arguments,
                    session_id=SESSION_ID,
                    enforce_workflow=True,
                )
            assert result == {"ready": True}
            assert (
                internal.get_registry.return_value.call.call_args.args[1]["session_id"]
                == SESSION_ID
            )
            provider_data: dict[str, Any] = (
                {"tool_use_id": f"call-{count}"} if omit_input else dict(data)
            )
            after = run._event(HookEventType.AFTER_TOOL, {**provider_data, "tool_output": result})
            after.source = source
            after.metadata.update(metadata)
            assert (await asyncio.to_thread(handler.evaluate, after)).decision == "allow"
            assert run.vars["completion_count"] == count
            rows = temp_db.fetchall(
                "SELECT id FROM workflow_audit_log WHERE session_id = %s AND event_type = %s "
                "AND tool_name = %s AND reason LIKE %s",
                (
                    SESSION_ID,
                    "tool_call",
                    "gobby-agents:wait_for_coordination",
                    "%completed successfully",
                ),
            )
            assert len(rows) == count
    finally:
        handler.shutdown()


@pytest.mark.parametrize("transition", [True, False])
async def test_stale_step_completion_logs_warning(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    transition: bool,
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    if not transition:
        await run.intake(task)

    def stale(*args: Any, **kwargs: Any) -> None:
        raise StaleStepInstanceWriteError("concurrent seat completion")

    monkeypatch.setattr(AgentStepInstanceManager, "save", stale)
    await run.intake(task)
    assert run.step == ("await" if transition else "review")
    assert "Stale step completion write" in caplog.text


@pytest.mark.parametrize("wrong", ["sender", "sha", "active-candidate"])
async def test_stale_session_cleanup_requires_exact_authorized_unbound_candidate(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, wrong: str
) -> None:
    patch_local_machine_id(monkeypatch, "21000000-0000-4000-8000-000000000001")
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    old = run.task()
    await run.intake(old)
    stale = dict(run.vars)
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    instance.variables.update(_CANDIDATE_DEFAULTS)
    instance.current_step = "await"
    run.instances.save(instance)
    if wrong == "active-candidate":
        replacement = run.task()
        await run.intake(replacement, REPLACEMENT_SHA)
    run.session_vars.update(stale)
    SessionVariableManager(temp_db).merge_variables(SESSION_ID, stale)
    await run.incoming(
        f"EVENT=CANDIDATE_WITHDRAWN TASK={old.id} SHA={REPLACEMENT_SHA if wrong == 'sha' else SHA}",
        sender=OWNER_ID if wrong == "sender" else COORDINATOR_ID,
    )
    assert run.session_vars["candidate_task"] == old.id
    assert run.vars["candidate_task"] == (replacement.id if wrong == "active-candidate" else None)
