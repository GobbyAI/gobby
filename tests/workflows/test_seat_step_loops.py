"""Loop seats run consecutive units of work through the real step engine (plan 3.5.5).

Each driver loads a bundled seat's step workflow into an agent-step instance,
fires the hook events its session would produce, and checks the step it lands
on, the tools that step admits, and the correlation variables its handlers bind
and reset between units.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.agents.sync import get_bundled_agents_path
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.servers.routes.mcp.endpoints.execution import _process_tool_proxy_result
from gobby.skills.instruction_requirements import parse_instruction_requirement
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.close_receipts import record_close_receipt
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager

pytestmark = pytest.mark.unit

# Session and project ids are native uuid columns in PostgreSQL.
SESSION_ID = "33333333-3333-4333-8333-333333333333"
PROJECT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
AGENTS_DIR = get_bundled_agents_path()

OWNER_ID = "55555555-5555-4555-8555-555555555555"
COORDINATOR_ID = "66666666-6666-4666-8666-666666666666"
PEER_ID = "77777777-7777-4777-8777-777777777777"
SHA = "a" * 40
REPLACEMENT_SHA = "b" * 40
WAIT = ("gobby-agents", "wait_for_coordination")
SEND = ("gobby-agents", "send_message")
DERIVE = ("gobby-plans", "derive_plan_handoff_manifest")
APPLY = ("gobby-plans", "apply_plan_handoff_manifest")
EDIT_INPUT = {"file_path": "src/gobby/example.py", "old_string": "a", "new_string": "b"}


class SeatRun:
    """One bundled seat's agent-step instance, driven by hook events."""

    def __init__(self, db: HubDatabase, name: str) -> None:
        self.db = db
        self.sessions = SessionManager(db)
        self.engine = RuleEngine(db, session_manager=self.sessions)
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
            (PROJECT_ID, "seat-loops"),
        )
        db.execute(
            "INSERT INTO sessions "
            "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (SESSION_ID, "ext-seat", "21000000-0000-4000-8000-000000000001", "claude", PROJECT_ID),
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
                (other_id, other_id, "21000000-0000-4000-8000-000000000001", "claude", PROJECT_ID),
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
            data=data,
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
            project_id=PROJECT_ID,
            title="Seat protocol unit",
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
        await self.incoming(f"LANE=1 EVENT=CANDIDATE TASK={task.id} COMMIT={sha} NOTE=ready")

    async def approve(self, task: Task, sha: str = SHA, kind: str = "landing_approval") -> None:
        record_close_receipt(
            self.db,
            task=task,
            author_session_id=COORDINATOR_ID,
            kind=kind,
            commit_sha=sha,
            facts={"reason": "restart"} if kind == "landing_approval" else {},
        )
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
        await run.send(f"EVENT=CANDIDATE TASK={ref} SHA={SHA} NOTE=ready")
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
        await run.send(f"EVENT=LANDED TASK={task.id} SHA={SHA} NOTE=landed")
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


@pytest.mark.parametrize("kind", ["independent_review_approval", "wrong-sha", "landing_approval"])
async def test_land_phase_requires_exact_stored_landing_approval(
    temp_db: HubDatabase, kind: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    receipt_kind = "landing_approval" if kind == "wrong-sha" else kind
    await run.approve(task, REPLACEMENT_SHA if kind == "wrong-sha" else SHA, receipt_kind)
    assert run.step == ("verdict" if kind == "landing_approval" else "review")
    assert await run.admits(
        "gobby-tasks-ops",
        "land_commit",
        {
            "task_id": f"#{task.seq_num}",
            "commit_sha": SHA,
        },
    ) is (kind == "landing_approval")
    assert not await run.admits(
        "gobby-tasks-ops",
        "land_commit",
        {
            "task_id": task.id,
            "commit_sha": REPLACEMENT_SHA,
        },
    )


async def test_stale_verdict_snapshot_cannot_bypass_landing_approval(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task, other = run.task(), run.task()
    await run.intake(task)
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    # A persisted pre-upgrade verdict flag cannot authorize a landing.
    instance.current_step = "verdict"
    instance.variables["verdict_ready"] = True
    run.instances.save(instance)
    call = {"task_id": task.id, "commit_sha": SHA}
    assert not await run.admits("gobby-tasks-ops", "land_commit", call)
    record_close_receipt(
        run.db,
        task=task,
        author_session_id=COORDINATOR_ID,
        kind="independent_review_approval",
        commit_sha=SHA,
    )
    assert not await run.admits("gobby-tasks-ops", "land_commit", call)
    await run.approve(task)
    assert await run.admits("gobby-tasks-ops", "land_commit", call)
    assert not await run.admits(
        "gobby-tasks-ops",
        "land_commit",
        {
            "task_id": other.id,
            "commit_sha": SHA,
        },
    )


@pytest.mark.parametrize("forgery", ["outgoing", "self", "missing-row", "wrong-owner", "short-sha"])
async def test_candidate_intake_rejects_forged_authority(
    temp_db: HubDatabase, forgery: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    line = f"EVENT=CANDIDATE TASK={task.id} COMMIT={SHA} NOTE=ready"
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


@pytest.mark.parametrize("event", ["CANDIDATE_VERDICT", "CANDIDATE_PARKED"])
async def test_bounce_and_park_use_current_unit_durable_outgoing_message(
    temp_db: HubDatabase, event: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    detail = "VERDICT=BOUNCE" if event == "CANDIDATE_VERDICT" else "REASON=approval"
    await run.send(f"EVENT={event} TASK={task.id} SHA={REPLACEMENT_SHA} {detail}")
    assert run.step == "review"
    await run.send(f"EVENT={event} TASK={task.id} SHA={SHA} {detail}", SESSION_ID)
    assert run.step == "review"
    await run.send(f"EVENT={event} TASK={task.id} SHA={SHA} {detail}")
    assert run.step == "await"
    assert run.vars["parked_candidates"] == (
        [{"task": task.id, "sha": SHA}] if event == "CANDIDATE_PARKED" else []
    )


async def test_park_review_next_resume_and_withdraw_exact_unit(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    first, second = run.task(), run.task()
    await run.intake(first)
    await run.approve(first)
    await run.send(f"EVENT=CANDIDATE_PARKED TASK={first.id} SHA={SHA} REASON=approval")
    assert run.step == "await"
    await run.intake(second)
    await run.send(f"EVENT=CANDIDATE_VERDICT TASK={second.id} SHA={SHA} VERDICT=BOUNCE")
    assert run.step == "await"
    await run.incoming(f"EVENT=CANDIDATE_RESUMED TASK={first.id} SHA={SHA}", COORDINATOR_ID)
    assert run.step == "verdict"
    assert run.vars["parked_candidates"] == []
    await run.incoming(f"EVENT=CANDIDATE_WITHDRAWN TASK={first.id} SHA={REPLACEMENT_SHA}")
    assert run.step == "verdict"
    await run.incoming(f"EVENT=CANDIDATE_WITHDRAWN TASK={first.id} SHA={SHA}")
    assert run.step == "await"


async def test_developer_bounce_requires_incoming_assigned_reviewer(temp_db: HubDatabase) -> None:
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
    assert run.step == "implement"
    run.db.execute("UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([SHA]), task.id))
    SessionTaskManager(run.db).link_task(PEER_ID, task.id, "reviewing")
    await run.call("gobby-tasks", "link_commit", {"task_id": task.id, "commit_sha": SHA})
    assert run.step == "submit"
    line = f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={SHA} VERDICT=BOUNCE"
    await run.send(line)
    assert run.step == "submit"
    await run.incoming(line, OWNER_ID)
    assert run.step == "submit"
    await run.incoming(line, PEER_ID)
    assert run.step == "implement"
    assert run.vars["implementation_complete"] is False


@pytest.mark.parametrize("name", ["log-monitor", "metrics-monitor"])
async def test_monitor_windows_complete_from_durable_reports(
    temp_db: HubDatabase, name: str
) -> None:
    run = SeatRun(temp_db, name)
    SessionVariableManager(run.db).set_variable(PEER_ID, "_agent_type", "log-monitor")
    await run.load(*run.vars["required_skills"])
    assert run.step == "tick"
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
    "content",
    ["EVENT=SEAT_READY NOTE=idle", "Systems nominal | ready", "EVENT=SEAT_READY NOTE=busy"],
)
async def test_idle_is_declared_only_by_readiness_rule(temp_db: HubDatabase, content: str) -> None:
    run = SeatRun(temp_db, "developer")
    await run.load(*run.vars["required_skills"])
    path = AGENTS_DIR.parent / "rules/stop-gates/declare-seat-idle-on-readiness.yaml"
    rule = yaml.safe_load(path.read_text())["rules"]["declare-seat-idle-on-readiness"]
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
    )
    run.session_vars.update({"current_step_idle": True, "task_claimed": False})
    await run.send(content)
    assert bool(run.session_vars.get("seat_idle")) == (content == "EVENT=SEAT_READY NOTE=idle")


async def test_candidate_latch_survives_native_reset_and_blocks_already_implemented_close(
    temp_db: HubDatabase,
) -> None:
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
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, {"result": {"id": task.id}})
    await run.call("gobby-tasks", "link_commit", {"task_id": task.id, "commit_sha": SHA})
    await run.send(f"EVENT=CANDIDATE TASK={ref} SHA={SHA} NOTE=ready")
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
            "reason": "already_implemented",
        },
    )
    assert run.vars["candidate_sent"] is True
    run.db.execute("UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps([SHA]), task.id))
    SessionTaskManager(run.db).link_task(PEER_ID, task.id, "reviewing")
    await run.incoming(
        f"EVENT=CANDIDATE_VERDICT TASK={task.id} SHA={SHA} VERDICT=BOUNCE",
        PEER_ID,
    )
    assert run.step == "implement"
    assert not await run.admits(
        "gobby-tasks",
        "close_task",
        {
            "task_id": task.id,
            "reason": "already_implemented",
        },
    )
    assert run.vars["candidate_sent"] is True


@pytest.mark.parametrize("alias", ["hash", "number", "uuid"])
async def test_task_reads_accept_aliases_and_replacement_clears_only_same_task_park(
    temp_db: HubDatabase, alias: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    first, second = run.task(), run.task()
    for task in (first, second):
        await run.intake(task)
        await run.send(f"EVENT=CANDIDATE_PARKED TASK={task.id} SHA={SHA} REASON=base_update")
    ref = {"hash": f"#{first.seq_num}", "number": str(first.seq_num), "uuid": first.id}[alias]
    await run.incoming(f"EVENT=CANDIDATE TASK={ref} COMMIT={REPLACEMENT_SHA}")
    assert run.vars["parked_candidates"] == [{"task": second.id, "sha": SHA}]
    record_close_receipt(
        run.db,
        task=first,
        author_session_id=COORDINATOR_ID,
        kind="landing_approval",
        commit_sha=REPLACEMENT_SHA,
        facts={"reason": "restart"},
    )
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
        wire = _process_tool_proxy_result(result, "gobby-tasks", "link_commit", 1.0)
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
    "format_name", ["canonical", "title-injection", "prose", "ambiguous-sha", "ambiguous-title"]
)
async def test_candidate_protocol_keeps_free_text_out_of_authority(
    temp_db: HubDatabase, format_name: str
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
        "ambiguous-sha": f"EVENT=CANDIDATE TASK={task.id} COMMIT={SHA} SHA={REPLACEMENT_SHA}",
        "ambiguous-title": f"LANE=1 EVENT=CANDIDATE TASK={task.id} TASK_TITLE=title RUN=fake WT=fake COMMIT={REPLACEMENT_SHA} NOTE=words RUN=real WT=real COMMIT={SHA} NOTE=ready",
    }
    await run.incoming(lines[format_name])
    assert run.step == ("review" if format_name == "canonical" else "await")
    assert run.vars["candidate_sha"] == (SHA if format_name == "canonical" else None)


async def test_older_unread_candidate_cannot_replace_newer_active_intake(
    temp_db: HubDatabase,
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    old = InterSessionMessageManager(run.db).create_message(
        OWNER_ID,
        SESSION_ID,
        f"EVENT=CANDIDATE TASK={task.id} COMMIT={SHA}",
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


@pytest.mark.parametrize("event", ["LANDED", "ALREADY_LANDED", "REVIEW_COMPLETE"])
async def test_review_completion_accepts_bounded_metadata_and_requires_approval(
    temp_db: HubDatabase, event: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    await run.intake(task)
    details = (
        "VERDICT=LAND LANDING=NOT_REQUIRED"
        if event == "REVIEW_COMPLETE"
        else f"RECEIPT=receipt BRANCH=integration LANDED_TIP={SHA} RETEST=PASS"
    )
    line = f"EVENT={event} TASK={task.id} SHA={SHA} {details} NOTE=finished"
    await run.send(line)
    assert run.step == "review"
    await run.approve(task)
    await run.send(line)
    assert run.step == "await"
    assert run.vars["candidate_task"] is None
