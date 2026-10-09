"""Loop seats run consecutive units of work through the real step engine (plan 3.5.5).

Each driver loads a bundled seat's step workflow into an agent-step instance,
fires the hook events its session would produce, and checks the step it lands
on, the tools that step admits, and the correlation variables its handlers bind
and reset between units.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.workflows._variables import set_variable
from gobby.skills.instruction_requirements import parse_instruction_requirement
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager

pytestmark = pytest.mark.unit

# Session and project ids are native uuid columns in PostgreSQL.
SESSION_ID = "33333333-3333-4333-8333-333333333333"
PROJECT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
AGENTS_DIR = get_bundled_agents_path()

WAIT = ("gobby-agents", "wait_for_coordination")
SEND = ("gobby-agents", "send_message")
DERIVE = ("gobby-plans", "derive_plan_handoff_manifest")
APPLY = ("gobby-plans", "apply_plan_handoff_manifest")
EDIT_INPUT = {"file_path": "src/gobby/example.py", "old_string": "a", "new_string": "b"}


class SeatRun:
    """One bundled seat's agent-step instance, driven by hook events."""

    def __init__(self, db: HubDatabase, name: str) -> None:
        self.db = db
        self.engine = RuleEngine(db)
        self.instances = AgentStepInstanceManager(db)
        self.sessions = SessionManager(db)
        self.session_vars: dict[str, Any] = {"loaded_skills": [], "loaded_skill_references": []}

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
        AgentDefinitionManager(db).create(
            name=name,
            definition_json=json.dumps(
                {
                    "name": name,
                    "variables": workflow["variables"],
                    "steps": workflow["steps"],
                    "exit_condition": workflow.get("exit_condition"),
                }
            ),
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

    async def admits(self, server: str, tool: str) -> bool:
        call = {"server_name": server, "tool_name": tool, "arguments": {}}
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
        assert await self.admits(server, tool), (self.step, server, tool)
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

    async def set_flag(self, name: str, value: str | bool | list[str]) -> None:
        """Write a step variable by hand through the real set_variable tool."""
        tool_input = {"name": name, "value": value, "scope": "step", "session_id": SESSION_ID}
        assert await self.admits_native("mcp__gobby__set_variable", tool_input), (self.step, name)
        output = set_variable(self.sessions, self.db, name, value, SESSION_ID, scope="step")
        assert output["success"] is True, output
        data = {
            "tool_name": "mcp__gobby__set_variable",
            "tool_input": tool_input,
            "tool_output": output,
        }
        await self._evaluate(self._event(HookEventType.AFTER_TOOL, data))


async def _developer(run: SeatRun) -> None:
    assert not await run.admits_native("Edit", EDIT_INPUT)
    await run.load(*run.vars["required_skills"])
    assert run.step == "claim"

    units = [
        ("44444444-4444-4444-8444-000000000101", "#101", "rust"),
        ("44444444-4444-4444-8444-000000000102", "#102", "typescript"),
    ]
    for task_id, ref, skill in units:
        assert await run.admits(*WAIT)
        await run.call(
            "gobby-tasks", "claim_task", {"task_id": ref}, {"result": {"task_id": task_id}}
        )
        assert run.step == "route_skills"
        assert run.vars["assigned_task_id"] == task_id
        assert not await run.admits(*WAIT)

        await run.call(
            "gobby-tasks", "get_task", {"task_id": ref}, {"result": {"id": task_id, "ref": ref}}
        )
        assert run.vars["assigned_task_ref"] == ref
        await run.set_flag("additional_skills", [skill])
        await run.set_flag("skills_routed", True)
        assert run.step == "load_additional_skills"
        assert not await run.admits_native("Edit", EDIT_INPUT)

        await run.load(skill)
        assert run.step == "implement"
        assert await run.admits_native("Edit", EDIT_INPUT)
        await run.call("gobby-tasks", "link_commit", {"task_id": ref, "commit_sha": "abc1234"})
        assert run.step == "submit"

        await run.call(*SEND, {"content": "EVENT=CANDIDATE TASK=#999"})
        assert run.vars["candidate_sent"] is False
        await run.call(*SEND, {"content": f"EVENT=CANDIDATE TASK={ref}"})
        assert run.vars["candidate_sent"] is True

        await run.call("gobby-tasks", "close_task", {"task_id": "#999"}, {"closed": True})
        assert run.step == "submit"
        assert run.vars["task_claimed"] is True
        await run.call("gobby-tasks", "close_task", {"task_id": ref}, {"closed": True})
        assert run.step == "claim"
        reset = {
            "task_claimed": False,
            "assigned_task_id": None,
            "assigned_task_ref": None,
            "skills_routed": False,
            "additional_skills_loaded": False,
            "implementation_complete": False,
            "candidate_sent": False,
        }
        assert {key: run.vars[key] for key in reset} == reset


async def _code_reviewer(run: SeatRun) -> None:
    await run.load(*run.vars["required_skills"])
    assert run.step == "await"

    title = "TASK_TITLE=Keep NOTE= and VERDICT=BOUNCE out of fields"
    reports = (
        ("#101", "EVENT=LANDED TASK=#101 SHA=abc RECEIPT=r1"),
        ("#102", "EVENT=CANDIDATE_VERDICT TASK=#102 SHA=abc VERDICT=BOUNCE NOTE=HIGH finding"),
        ("#103", f"EVENT=LANDED TASK=#103 {title} SHA=abc RECEIPT=r1"),
        ("#104", f"EVENT=CANDIDATE_VERDICT TASK=#104 {title} SHA=abc VERDICT=BOUNCE NOTE=HIGH"),
    )
    for task, report in reports:
        assert await run.admits(*WAIT)
        await run.set_flag("candidate_task", task)
        await run.set_flag("candidate_received", True)
        assert run.step == "review"
        assert not await run.admits(*WAIT)

        await run.set_flag("verdict_ready", True)
        assert run.step == "verdict"
        # Prose mentions of the terminal tokens or of the task, title words, and a
        # terminal line that does not start the message leave the unit open.
        for content in (
            f" {report}",
            f"\n{report}",
            f"EVENT=CANDIDATE_VERDICT TASK={task} TASK_TITLE=Retry VERDICT=BOUNCE handling "
            f"SHA=abc VERDICT=LAND NOTE=ok",
            f"EVENT=LANDED TASK=#999 TASK_TITLE=Follow-up TASK={task} SHA=abc",
            f"EVENT=CANDIDATE_VERDICT TASK={task} SHA=abc VERDICT=LAND "
            f"NOTE=EVENT=LANDED follows; no VERDICT=BOUNCE needed",
            f"Reviewer note: EVENT=LANDED TASK={task} follows",
            f"EVENT=LANDED TASK=#999 SHA=abc NOTE=landed after TASK={task}",
            f"EVENT=LANDED TASK=#999 TASK_TITLE='Follow-up to {task}' SHA=abc",
            f"EVENT=LANDED TASK={task}9 SHA=abc",
        ):
            await run.call(*SEND, {"content": content})
            assert run.step == "verdict"
            assert run.vars["candidate_task"] == task
            assert await run.admits("gobby-tasks-ops", "land_commit")

        await run.call(*SEND, {"content": report})
        assert run.step == "await"
        reset = {"candidate_received": False, "verdict_ready": False, "candidate_task": None}
        assert {key: run.vars[key] for key in reset} == reset


async def _log_monitor(run: SeatRun) -> None:
    assert not await run.admits(*WAIT)
    await run.load(*run.vars["required_skills"])
    assert run.step == "tick"

    for window, report in (("w1", "Systems nominal | w1"), ("w2", "EVENT=ALARM WINDOW=w2")):
        assert await run.admits(*WAIT)
        await run.set_flag("tick_window", window)
        await run.set_flag("tick_done", True)
        assert run.step == "report"

        await run.call(*SEND, {"content": f"digest {window}"})
        assert run.step == "report"
        assert (run.vars["tick_done"], run.vars["tick_window"]) == (True, window)

        await run.call(*SEND, {"content": report})
        assert run.step == "tick"
        assert (run.vars["tick_done"], run.vars["tick_window"]) == (False, None)


async def _plan_adversary(run: SeatRun) -> None:
    assert not await run.admits(*DERIVE)
    await run.load(*run.vars["required_skills"])
    assert run.step == "review"
    assert not await run.admits(*DERIVE)

    await run.call(*SEND, {"content": "EVENT=FINDINGS ROUND=1"})
    assert run.step == "review"
    await run.call(*SEND, {"content": "EVENT=CONSENSUS ROUND=1"})
    assert run.step == "stamp"
    assert await run.admits(*DERIVE)
    assert await run.admits(*APPLY)


DRIVERS: dict[str, Callable[[SeatRun], Awaitable[None]]] = {
    "developer": _developer,
    "code-reviewer": _code_reviewer,
    "log-monitor": _log_monitor,
    "plan-adversary": _plan_adversary,
}


@pytest.mark.parametrize("name", sorted(DRIVERS))
async def test_loop_seats_reset_between_units(temp_db: HubDatabase, name: str) -> None:
    run = SeatRun(temp_db, name)
    await DRIVERS[name](run)
    assert run.step in {"claim", "await", "tick", "stamp"}
