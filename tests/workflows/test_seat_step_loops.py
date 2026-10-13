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

from gobby.adapters.claude_code import ClaudeCodeAdapter
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
            definition_json=json.dumps(agent),
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
        assert await run.admits(*WAIT)

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
    await run.call(*SEND, {"content": f"EVENT=CONSENSUS SHA={'a' * 40}"})
    assert run.step == "stamp"
    assert await run.admits(*DERIVE)
    assert await run.admits(*APPLY)


@pytest.mark.parametrize(
    "content",
    [
        "EVENT=CONSENSUS",
        "EVENT=CONSENSUS ROUND=1",
        f"Cannot send EVENT=CONSENSUS SHA={'a' * 40}; no committed V1 entry exists.",
        f"EVENT=CONSENSUS_EXTRA SHA={'a' * 40}",
        "EVENT=CONSENSUS SHA=not-a-commit",
        "EVENT=CONSENSUS SHA=",
        "EVENT=CONSENSUS SHA=abcdef",
        f"EVENT=CONSENSUS SHA={'a' * 41}",
        f"EVENT=CONSENSUS\nSHA={'a' * 40}",
    ],
)
async def test_plan_adversary_consensus_requires_event_line_and_sha(
    temp_db: HubDatabase, content: str
) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])

    await run.call(*SEND, {"content": content})

    assert run.step == "review"
    assert run.vars["consensus"] is False
    assert not await run.admits(*DERIVE)


@pytest.mark.parametrize(
    "content",
    [
        "EVENT=CONSENSUS SHA=ABC1234",
        f"EVENT=CONSENSUS ROUND=1 SHA={'a' * 40}",
        f"Verified the committed V1 entry.\nEVENT=CONSENSUS SHA={'a' * 40}\n",
    ],
)
async def test_plan_adversary_committed_consensus_enters_stamp(
    temp_db: HubDatabase, content: str
) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])

    await run.call(*SEND, {"content": content})

    assert run.step == "stamp"
    assert await run.admits(*DERIVE)
    assert await run.admits(*APPLY)


async def test_plan_adversary_failed_consensus_send_stays_in_review(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])

    await run.call(*SEND, {"content": f"EVENT=CONSENSUS SHA={'a' * 40}"}, {"success": False})

    assert run.step == "review"
    assert run.vars["consensus"] is False
    assert not await run.admits(*DERIVE)


async def test_plan_adversary_stamp_bounce_returns_to_review(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    consensus = {"content": f"EVENT=CONSENSUS SHA={'a' * 40}"}
    await run.call(*SEND, consensus)
    assert run.step == "stamp"

    await run.set_flag("bounced", True)

    assert run.step == "review"
    assert run.vars["bounced"] is True
    assert not await run.admits(*DERIVE)
    await run.call(*SEND, {"content": "Review is still in progress."})
    assert run.step == "review"
    await run.call(*SEND, consensus)
    assert run.step == "stamp"
    assert run.vars["bounced"] is False


async def test_plan_adversary_old_stamp_snapshot_uses_current_bounce(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "plan-adversary")
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
    old_stamp.transitions = []
    instance.snapshot.variables.pop("bounced")
    instance.variables.pop("bounced")
    instance.variables["consensus"] = True
    instance.current_step = "stamp"
    run.instances.replace_for_session(instance)

    await run.set_flag("bounced", True)

    assert run.step == "review"
    assert not await run.admits(*DERIVE)
    retained = run.instances.get_for_session(SESSION_ID)
    assert retained is not None
    retained_stamp = retained.snapshot.get_step("stamp")
    assert retained_stamp is not None
    assert retained_stamp.transitions == []


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


@pytest.mark.parametrize("envelope", ["direct", "content", "structured"])
async def test_code_reviewer_failed_verdict_send_keeps_unit(
    temp_db: HubDatabase, envelope: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    await run.set_flag("candidate_task", "#101")
    await run.set_flag("candidate_received", True)
    await run.set_flag("verdict_ready", True)
    report = "EVENT=CANDIDATE_VERDICT TASK=#101 SHA=abc VERDICT=BOUNCE NOTE=HIGH finding"
    failure: dict[str, Any] = {
        "success": False,
        "error": "health check did not respond within 2s",
        "code": "DAEMON_UNAVAILABLE",
    }
    output = failure
    if envelope == "content":
        output = {"isError": False, "content": [{"type": "text", "text": json.dumps(failure)}]}
    elif envelope == "structured":
        output = {"isError": False, "structuredContent": failure}

    adapter = ClaudeCodeAdapter()
    native_event: dict[str, Any] = {
        "hook_type": "post-tool-use",
        "input_data": {
            "session_id": SESSION_ID,
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": SEND[0],
                "tool_name": SEND[1],
                "arguments": {"content": report},
            },
            "tool_response": output,
        },
    }
    event = adapter.translate_to_hook_event(native_event)
    await run._evaluate(event)
    assert run.step == "verdict"
    assert run.vars["candidate_task"] == "#101"
    assert run.vars["candidate_received"] is True
    assert run.vars["verdict_ready"] is True

    native_event["input_data"]["tool_response"] = {"success": True}
    await run._evaluate(adapter.translate_to_hook_event(native_event))
    assert run.step == "await"
    assert run.vars["candidate_task"] is None


@pytest.mark.parametrize("reason", ["approval", "base_update", "review_only"])
async def test_code_reviewer_parks_blocked_land_reviews_next_and_resumes(
    temp_db: HubDatabase, reason: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    first_sha, second_sha = "a" * 40, "b" * 40
    await run.set_flag("candidate_task", "#24224")
    await run.set_flag("candidate_sha", first_sha)
    await run.set_flag("candidate_received", True)
    await run.call(
        "gobby-tasks", "link_task_to_session", {"task_id": "#24224", "action": "reviewing"}
    )
    await run.set_flag("verdict_ready", True)
    assert run.step == "verdict"
    assert await run.admits("gobby-tasks", "link_task_to_session")

    park = f"EVENT=CANDIDATE_PARKED TASK=#24224 SHA={first_sha} REASON={reason} NOTE=blocked"
    await run.call(*SEND, {"content": park}, {"success": False})
    assert run.step == "verdict"
    active = run.vars
    for invalid in (
        park.replace("TASK=#24224", "TASK=#999"),
        park.replace(first_sha, second_sha),
        park.replace(f"REASON={reason}", "REASON=merge_conflict"),
        "Prose " + park,
    ):
        await run.call(*SEND, {"content": invalid})
        assert run.step == "verdict"
        assert run.vars == active
    await run.call(*SEND, {"content": park})
    assert run.step == "await"
    assert run.vars["parked_candidates"] == [{"task": "#24224", "sha": first_sha}]
    assert run.vars["candidate_task"] is None
    assert run.vars["candidate_sha"] is None

    await run.set_flag("candidate_task", "#24234")
    await run.set_flag("candidate_sha", second_sha)
    await run.set_flag("candidate_received", True)
    assert run.step == "review"
    await run.call(
        "gobby-tasks", "link_task_to_session", {"task_id": "#24234", "action": "reviewing"}
    )
    await run.set_flag("verdict_ready", True)
    await run.call(
        *SEND, {"content": f"EVENT=CANDIDATE_VERDICT TASK=#24234 SHA={second_sha} VERDICT=BOUNCE"}
    )
    assert run.step == "await"
    assert run.vars["parked_candidates"] == [{"task": "#24224", "sha": first_sha}]

    resume = f"EVENT=CANDIDATE_RESUMED TASK=#24224 SHA={first_sha} NOTE=approval arrived"
    parked = run.vars
    for invalid in (
        resume.replace("TASK=#24224", "TASK=#999"),
        resume.replace(first_sha, second_sha),
        "Prose " + resume,
    ):
        await run.call(*SEND, {"content": invalid})
        assert run.step == "await"
        assert run.vars == parked
    await run.call(*SEND, {"content": resume}, {"success": False})
    assert run.step == "await"
    await run.call(*SEND, {"content": resume})
    assert run.step == "verdict"
    assert run.vars["candidate_task"] == "#24224"
    assert run.vars["candidate_sha"] == first_sha
    assert run.vars["parked_candidates"] == []
    assert run.vars["verdict_ready"] is True
    assert await run.admits("gobby-tasks", "link_task_to_session")
    await run.call(
        "gobby-tasks", "link_task_to_session", {"task_id": "#24224", "action": "reviewing"}
    )
    assert run.step == "verdict"
    assert run.vars["candidate_sha"] == first_sha
    assert run.vars["verdict_ready"] is True
    if reason == "review_only":
        for wrong_sha in ("", second_sha):
            await run.call(
                *SEND,
                {
                    "content": f"EVENT=REVIEW_COMPLETE TASK=#24224 SHA={wrong_sha} VERDICT=LAND LANDING=NOT_REQUIRED"
                },
            )
            assert run.step == "verdict"
            assert run.vars["candidate_sha"] == first_sha
        await run.call(
            *SEND,
            {
                "content": f"EVENT=REVIEW_COMPLETE TASK=#24224 SHA={first_sha} VERDICT=LAND LANDING=NOT_REQUIRED"
            },
        )
    else:
        await run.call(
            "gobby-tasks-ops", "land_commit", {"task_id": "#24224", "commit_sha": first_sha}
        )
        await run.call(*SEND, {"content": f"EVENT=LANDED TASK=#24224 SHA={first_sha} RECEIPT=real"})
    assert run.step == "await"
    assert run.vars["candidate_sha"] is None
    assert run.vars["_unit_done"] is False
    assert run.vars["_unit_parked"] is False


async def test_code_reviewer_replacement_sha_reenters_fresh_review(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    old_sha, new_sha = "a" * 40, "b" * 40
    await run.set_flag("candidate_task", "#24224")
    await run.set_flag("candidate_sha", old_sha)
    await run.set_flag("candidate_received", True)
    await run.set_flag("verdict_ready", True)
    assert run.step == "verdict"

    # Follow the replacement intake protocol instead of editing the approved SHA in verdict.
    await run.set_flag("verdict_ready", False)
    await run.set_flag("candidate_received", False)
    assert run.step == "await"
    await run.set_flag("candidate_task", "#24224")
    await run.set_flag("candidate_sha", new_sha)
    await run.set_flag("candidate_received", True)
    assert run.step == "review"
    assert run.vars["candidate_sha"] == new_sha
    assert run.vars["verdict_ready"] is False
    assert not await run.admits("gobby-tasks-ops", "land_commit")
    assert not await run.admits("gobby-tasks", "record_close_receipt")
    await run.call(
        "gobby-tasks", "link_task_to_session", {"task_id": "#24224", "action": "reviewing"}
    )
    await run.set_flag("verdict_ready", True)
    assert run.step == "verdict"
    await run.call(
        "gobby-tasks",
        "record_close_receipt",
        {"task_id": "#24224", "kind": "independent_review_approval", "commit_sha": new_sha},
    )
    assert run.vars["candidate_sha"] == new_sha


async def test_code_reviewer_fresh_intake_supersedes_only_same_task_parked_sha(
    temp_db: HubDatabase,
) -> None:
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    old_sha, other_sha, new_sha = "a" * 40, "b" * 40, "c" * 40
    for task, sha in (("#24224", old_sha), ("#24234", other_sha)):
        await run.set_flag("candidate_task", task)
        await run.set_flag("candidate_sha", sha)
        await run.set_flag("candidate_received", True)
        await run.set_flag("verdict_ready", True)
        await run.call(
            *SEND,
            {"content": f"EVENT=CANDIDATE_PARKED TASK={task} SHA={sha} REASON=base_update"},
        )
        assert run.step == "await"
    parked = run.vars["parked_candidates"]
    assert len(parked) == 2
    await run.set_flag("candidate_task", "#24224")
    await run.set_flag("candidate_sha", new_sha)
    await run.set_flag("candidate_received", True)
    assert run.step == "review"
    link = {"task_id": "#24224", "action": "reviewing"}
    await run.call("gobby-tasks", "link_task_to_session", link, {"success": False})
    assert run.vars["parked_candidates"] == parked
    await run.call(
        "gobby-tasks", "link_task_to_session", {"task_id": "#24234", "action": "reviewing"}
    )
    assert run.vars["parked_candidates"] == parked
    await run.call("gobby-tasks", "link_task_to_session", link)
    assert run.vars["parked_candidates"] == [{"task": "#24234", "sha": other_sha}]
    await run.set_flag("verdict_ready", True)
    await run.call(
        *SEND, {"content": f"EVENT=CANDIDATE_VERDICT TASK=#24224 SHA={new_sha} VERDICT=BOUNCE"}
    )
    await run.call(
        *SEND, {"content": f"EVENT=CANDIDATE_RESUMED TASK=#24224 SHA={old_sha} NOTE=obsolete"}
    )
    assert run.step == "await"
    assert run.vars["candidate_received"] is False
    assert run.vars["parked_candidates"] == [{"task": "#24234", "sha": other_sha}]
