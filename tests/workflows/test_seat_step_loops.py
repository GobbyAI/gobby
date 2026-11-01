"""Declared workflows preserve waits, message isolation and exactly-once completion.

Synthetic step graphs test engine behavior independently of bundled task standards.
Candidate-looking text remains opaque; monitor and consensus handlers require the
corresponding definition grants. Inline rules exercise stop and tool admission.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.agents.runner import AgentRunner
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.servers.routes.mcp.endpoints.execution import _process_tool_proxy_result
from gobby.skills.instruction_requirements import parse_instruction_requirement
from gobby.storage.coordination_waits import CoordinationWaitManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import (
    AgentStepInstance,
    AgentStepInstanceManager,
    StaleStepInstanceWriteError,
)

pytestmark = pytest.mark.unit

_CANDIDATE_DEFAULTS = {
    "candidate_received": False,
    "verdict_ready": False,
    "candidate_task": None,
    "candidate_sha": None,
    "candidate_owner": None,
    "candidate_received_at": None,
}


async def test_candidate_message_does_not_bind_or_move_claim(temp_db: HubDatabase) -> None:
    """Legacy-looking messages cannot advance a workflow or transfer its task claim."""
    run = SeatRun(temp_db, "code-reviewer")
    await run.load(*run.vars["required_skills"])
    task = run.task()
    before = LocalTaskManager(temp_db).get_task(task.id)
    await run.intake(task)
    after = LocalTaskManager(temp_db).get_task(task.id)
    assert run.vars["candidate_received"] is False
    assert run.step == "await"
    assert before is not None and after is not None
    assert after.claimed_by_session_id == before.claimed_by_session_id


# Session and project ids are native uuid columns in PostgreSQL.
SESSION_ID = "33333333-3333-4333-8333-333333333333"
PROJECT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _advance(server: str, tool: str) -> dict[str, str]:
    """Route one successful call through the engine's seat protocol handler."""
    return {"server": server, "tool": tool, "action": "advance_seat"}


# Synthetic seat definitions: only the variables, transitions and handlers the tests assert.
_SYNTHETIC_SEATS: dict[str, dict[str, Any]] = {
    "code-reviewer": {
        "name": "code-reviewer",
        "task_attestation_writes": ["review"],
        "prompts": {"agent": "Synthetic code-reviewer seat."},
        "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        "step_workflow": {
            "variables": {
                **_CANDIDATE_DEFAULTS,
                "required_skills": [],
                "fixing": False,
                "candidate_disposition": None,
            },
            "steps": [
                {
                    "name": "await",
                    "on_mcp_success": [
                        _advance("gobby-agents", "send_message"),
                        _advance("gobby-agents", "get_inter_session_message"),
                    ],
                    "transitions": [
                        {"to": "verdict", "when": "vars.candidate_received and vars.verdict_ready"},
                        {"to": "review", "when": "vars.candidate_received"},
                    ],
                },
                {
                    "name": "review",
                    "on_mcp_success": [
                        _advance("gobby-agents", "get_inter_session_message"),
                        _advance("gobby-agents", "send_message"),
                        _advance("gobby-tasks", "get_task"),
                        _advance("gobby-tasks", "record_review_step"),
                    ],
                    "transitions": [
                        {"to": "await", "when": "not vars.candidate_received"},
                        {"to": "verdict", "when": "vars.verdict_ready"},
                    ],
                },
                {
                    "name": "verdict",
                    "on_mcp_success": [
                        _advance("gobby-agents", "get_inter_session_message"),
                        _advance("gobby-agents", "send_message"),
                    ],
                    "transitions": [
                        {
                            "to": "review",
                            "when": "vars.candidate_received and not vars.verdict_ready",
                        },
                        {"to": "await", "when": "not vars.candidate_received"},
                    ],
                },
            ],
        },
    },
    "developer": {
        "name": "developer",
        "task_attestation_writes": ["implementation"],
        "prompts": {"agent": "Synthetic developer seat."},
        "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        "step_workflow": {
            "variables": {
                "required_skills": [],
                "required_skills_loaded": False,
                "assigned_task_id": None,
                "assigned_task_ref": None,
                "implementation_complete": False,
                "candidate_sent": False,
                "bounced": False,
                "pending_completion_tasks": [],
                "additional_skills": [],
                "skills_routed": False,
            },
            "steps": [
                {
                    "name": "claim",
                    "on_mcp_success": [
                        {
                            "server": "gobby-tasks",
                            "tool": "claim_task",
                            "action": "set_variable",
                            "variable": "assigned_task_id",
                            "value": "(tool_output.get('result') or tool_output).get('task_id')",
                        },
                        {
                            "server": "gobby-tasks",
                            "tool": "claim_task",
                            "action": "set_variable",
                            "variable": "assigned_task_ref",
                            "value": "(tool_output.get('result') or tool_output).get('ref')",
                        },
                    ],
                    "transitions": [{"to": "route_skills", "when": "vars.assigned_task_id"}],
                },
                {
                    "name": "route_skills",
                    "on_mcp_success": [_advance("gobby-tasks", "get_task")],
                    "transitions": [
                        {"to": "load_additional_skills", "when": "vars.additional_skills"},
                        {
                            "to": "implement",
                            "when": "vars.skills_routed and not vars.additional_skills",
                        },
                    ],
                },
                {
                    "name": "load_additional_skills",
                    "transitions": [
                        {
                            "to": "implement",
                            "when": "all(skill_loaded(skill) for skill in vars.additional_skills)",
                        }
                    ],
                },
                {
                    "name": "implement",
                    "on_mcp_success": [
                        {
                            "server": "gobby-tasks",
                            "tool": "link_commit",
                            "action": "set_variable",
                            "variable": "implementation_complete",
                            "value": True,
                        },
                        {
                            "server": "gobby-tasks",
                            "tool": "link_commit",
                            "action": "set_variable",
                            "variable": "bounced",
                            "value": False,
                        },
                    ],
                    "transitions": [{"to": "submit", "when": "vars.implementation_complete"}],
                },
                {
                    "name": "submit",
                    "on_mcp_success": [_advance("gobby-agents", "get_inter_session_message")],
                    "transitions": [{"to": "implement", "when": "vars.bounced"}],
                },
            ],
        },
    },
    "plan-adversary": {
        "name": "plan-adversary",
        "capabilities": ["track_plan_consensus"],
        "prompts": {"agent": "Synthetic plan-adversary seat."},
        "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        "step_workflow": {
            "variables": {
                "required_skills": [],
                "consensus": False,
                "consensus_sha": None,
                "bounced": False,
            },
            "steps": [
                {
                    "name": "review",
                    "on_mcp_success": [
                        _advance("gobby-agents", "send_message"),
                        _advance("gobby-agents", "get_inter_session_message"),
                    ],
                    "transitions": [{"to": "stamp", "when": "vars.consensus"}],
                },
                {
                    "name": "stamp",
                    "on_mcp_success": [
                        _advance("gobby-agents", "send_message"),
                        _advance("gobby-agents", "get_inter_session_message"),
                    ],
                    "transitions": [{"to": "review", "when": "vars.bounced"}],
                },
            ],
        },
    },
    "log-monitor": {
        "name": "log-monitor",
        "monitor_report_recipients": ["triage"],
        "prompts": {"agent": "Synthetic log-monitor seat."},
        "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        "step_workflow": {
            "variables": {
                "required_skills": [],
                "tick_done": False,
                "tick_window": None,
                "_window_report": None,
                "_reported_to": [],
            },
            "steps": [
                {"name": "tick", "on_mcp_success": [_advance("gobby-agents", "send_message")]}
            ],
        },
    },
    "metrics-monitor": {
        "name": "metrics-monitor",
        "monitor_report_recipients": ["triage", "log-monitor"],
        "prompts": {"agent": "Synthetic metrics-monitor seat."},
        "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        "step_workflow": {
            "variables": {
                "required_skills": [],
                "tick_done": False,
                "tick_window": None,
                "_window_report": None,
                "_reported_to": [],
            },
            "steps": [
                {
                    "name": "tick",
                    "on_mcp_success": [_advance("gobby-agents", "send_message")],
                    "transitions": [{"to": "report", "when": "vars.tick_done"}],
                },
                {
                    "name": "report",
                    "on_mcp_success": [_advance("gobby-agents", "send_message")],
                    "transitions": [{"to": "tick", "when": "not vars.tick_done"}],
                },
            ],
        },
    },
}

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


# Inline stop gate: a reduced copy of the stop condition, not the bundled
# require-step-completion rule. Stop tests here check the engine's stop handling only.
_STOP_RULE = "inline-seat-stop-gate"
# Inline copy of the readiness condition. The bundled declare-seat-idle-on-readiness
# rule is not loaded by any test here.
_READY_RULE = "inline-seat-readiness"
_READY_WHEN = (
    "variables.get('current_step_idle') and not variables.get('task_claimed') "
    "and event.data.get('mcp_server') == 'gobby-agents' "
    "and event.data.get('mcp_tool') == 'send_message' "
    "and event.data.get('tool_outcome', {}).get('status') == 'succeeded' "
    "and not event.metadata.get('is_failure') "
    "and seat_readiness_completed(event.metadata.get('_platform_session_id', ''), "
    "event.data.get('tool_output'))"
)


def _set(server: str, tool: str, variable: str, value: Any) -> dict[str, Any]:
    return {
        "server": server,
        "tool": tool,
        "action": "set_variable",
        "variable": variable,
        "value": value,
    }


def _seat(steps: dict[str, dict[str, list[Any]]], base: str = "developer") -> dict[str, Any]:
    """A copy of a synthetic seat with inline entries added to named steps.

    List-valued step keys are extended; a key absent from the base is created.
    """
    body: dict[str, Any] = json.loads(json.dumps(_SYNTHETIC_SEATS[base]))
    # Inline rules are selected by tag; the synthetic seats select no rules by default.
    body["workflows"]["rule_selectors"] = {"include": ["tag:inline"], "exclude": []}
    named = {entry["name"]: entry for entry in body["step_workflow"]["steps"]}
    for step, additions in steps.items():
        for key, values in additions.items():
            named[step].setdefault(key, []).extend(values)
    return body


def _idle_seat(base: str, step: str) -> dict[str, Any]:
    """A synthetic seat whose named step is the idle step a standing seat may stop at."""
    body = _seat({}, base=base)
    for entry in body["step_workflow"]["steps"]:
        if entry["name"] == step:
            entry["idle"] = True
    return body


_LATCH_SEAT = _seat(
    {
        "submit": {
            "on_mcp_success": [
                _set("gobby-agents", "send_message", "candidate_sent", True),
                _set(
                    "gobby-agents",
                    "get_inter_session_message",
                    "candidate_sent",
                    "False if 'EVENT=PROOF_REQUEST' in str(tool_output) else vars.candidate_sent",
                ),
            ]
        }
    }
)


def _create_rule(run: SeatRun, name: str, body: dict[str, Any], *, priority: int) -> None:
    run.engine.rule_manager.create(
        name=name,
        definition_json=RuleDefinitionBody.model_validate(body).model_dump_json(),
        enabled=True,
        priority=priority,
        tags=["inline"],
    )


def _stop_event(cwd: Path) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.STOP,
        session_id="ext-seat",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        cwd=str(cwd),
        data={},
        metadata={"_platform_session_id": SESSION_ID},
    )


def _stop_handler(run: SeatRun) -> WorkflowHookHandler:
    """Inline stop rules: a seat may stop once declared idle or holding a durable wait."""
    _create_rule(
        run,
        _READY_RULE,
        {
            "event": "after_tool",
            "when": _READY_WHEN,
            "effects": [{"type": "set_variable", "variable": "seat_idle", "value": True}],
        },
        priority=6,
    )
    _create_rule(
        run,
        _STOP_RULE,
        {
            "event": "turn_end",
            "when": "not variables.get('seat_idle') and not has_durable_stop_wait()",
            "effects": [{"type": "block", "reason": _STOP_RULE}],
        },
        priority=50,
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


class SeatRun:
    """One seat's agent-step instance, driven by hook events."""

    def __init__(
        self,
        db: HubDatabase,
        name: str,
        *,
        project_id: str = PROJECT_ID,
        definition: dict[str, Any] | None = None,
    ) -> None:
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

        agent = json.loads(json.dumps(definition or _SYNTHETIC_SEATS[name]))
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
                "capabilities": ["manage_task_claims"],
                "task_attestation_writes": ["review"],
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


async def test_inline_before_tool_rule_refuses_native_latch_reset(
    temp_db: HubDatabase,
) -> None:
    run = SeatRun(temp_db, "developer", definition=_LATCH_SEAT)
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    instance.variables["candidate_sent"] = True
    run.instances.save(instance)
    assert run.vars["candidate_sent"] is True
    _create_rule(
        run,
        "inline-candidate-latch-native",
        {
            "event": "before_tool",
            "when": (
                "event.data.get('tool_name') == 'mcp__gobby__set_variable' "
                "and event.data.get('tool_input', {}).get('name') == 'candidate_sent' "
                "and event.data.get('tool_input', {}).get('value') is False"
            ),
            "effects": [{"type": "block", "reason": "The declared latch is protected."}],
        },
        priority=10,
    )
    assert not await run.admits_native(
        "mcp__gobby__set_variable",
        {"name": "candidate_sent", "value": False, "scope": "step", "session_id": SESSION_ID},
    )
    assert run.vars["candidate_sent"] is True


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
    idle_step = "claim" if name == "developer" else "await"
    run = SeatRun(temp_db, name, definition=_idle_seat(name, idle_step))
    await run.load(*run.vars["required_skills"])
    run.session_vars.update({"current_step_idle": True, "task_claimed": False})
    assert run.step == idle_step
    handler = _stop_handler(run)
    blocked = await handler.evaluate_async(_stop_event(tmp_path))
    assert blocked.decision == "block" and _STOP_RULE in (blocked.reason or "")

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
    stopped = await handler.evaluate_async(_stop_event(tmp_path))
    assert stopped.decision == ("allow" if ready else "block"), stopped.reason
    assert bool(SessionVariableManager(temp_db).get_variables(SESSION_ID).get("seat_idle")) is ready
    assert run.step == idle_step


@pytest.mark.parametrize("step", ["await", "review"])
async def test_reviewer_registered_coordination_wait_allows_stop_with_external_session_id(
    temp_db: HubDatabase, tmp_path: Path, step: str
) -> None:
    run = SeatRun(temp_db, "code-reviewer", definition=_seat({}, base="code-reviewer"))
    await run.load(*run.vars["required_skills"])
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    instance.current_step = step
    run.instances.save(instance)
    assert run.step == step
    handler = _stop_handler(run)
    blocked = await handler.evaluate_async(_stop_event(tmp_path))
    assert blocked.decision == "block" and _STOP_RULE in (blocked.reason or "")
    wait = CoordinationWaitManager(temp_db).register(SESSION_ID, COORDINATOR_ID, reply=True)
    assert wait["outcome"] == "waiting"
    stopped = await handler.evaluate_async(_stop_event(tmp_path))
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
    run = SeatRun(temp_db, name, definition=_seat({}, base=name))
    await run.load(*run.vars["required_skills"])
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    instance.current_step = step
    run.instances.save(instance)
    assert run.step == step
    handler = _stop_handler(run)
    blocked = await handler.evaluate_async(_stop_event(tmp_path))
    assert blocked.decision == "block" and _STOP_RULE in (blocked.reason or "")
    arguments = {"owner_session": COORDINATOR_ID, "reply": True}
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
    stopped = await handler.evaluate_async(_stop_event(tmp_path))
    assert stopped.decision == ("allow" if wait_state == "active" else "block"), stopped.reason
    assert not SessionVariableManager(temp_db).get_variables(SESSION_ID).get("seat_idle")


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
async def test_inline_readiness_rule_declares_idle_only_for_a_ready_report(
    temp_db: HubDatabase, content: str, failure: str
) -> None:
    run = SeatRun(temp_db, "developer", definition=_seat({}))
    await run.load(*run.vars["required_skills"])
    _create_rule(
        run,
        _READY_RULE,
        {
            "event": "after_tool",
            "when": _READY_WHEN,
            "effects": [{"type": "set_variable", "variable": "seat_idle", "value": True}],
        },
        priority=6,
    )
    run.session_vars.update({"current_step_idle": True, "task_claimed": False})
    # The engine takes the caller's platform identity from the session it evaluates, so an
    # identity mismatch is a readiness send that another session made.
    sender, recipient = (
        (COORDINATOR_ID, SESSION_ID) if failure == "identity" else (SESSION_ID, COORDINATOR_ID)
    )
    message = InterSessionMessageManager(temp_db).create_message(sender, recipient, content)
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
    await run._evaluate(event)
    assert bool(run.session_vars.get("seat_idle")) == (
        content == "EVENT=SEAT_READY NOTE=idle" and not failure
    )


@pytest.mark.parametrize("name", ["plan-adversary", "fixture-plan-review"])
@pytest.mark.parametrize("grant", [True, False])
@pytest.mark.parametrize("monitor", [True, False])
async def test_consensus_tracking_requires_declared_capability(
    temp_db: HubDatabase, name: str, grant: bool, monitor: bool
) -> None:
    definition = json.loads(json.dumps(_SYNTHETIC_SEATS["plan-adversary"]))
    definition.update(name=name, capabilities=["track_plan_consensus"] if grant else [])
    definition["monitor_report_recipients"] = ["fixture-observer"] if monitor else []
    run = SeatRun(temp_db, name, definition=definition)
    await run.load(*run.vars["required_skills"])
    await run.send(f"EVENT=CONSENSUS SHA={SHA} NOTE=approved")
    assert run.vars["consensus"] is grant
    assert run.step == ("stamp" if grant else "review")


@pytest.mark.parametrize("target_enabled", [True, False])
async def test_monitor_routing_uses_declared_recipients(
    temp_db: HubDatabase, target_enabled: bool
) -> None:
    definition = json.loads(json.dumps(_SYNTHETIC_SEATS["metrics-monitor"]))
    targets = ["fixture-observer-a", "fixture-observer-b"]
    definition.update(name="fixture-monitor", monitor_report_recipients=targets)
    run = SeatRun(temp_db, "fixture-monitor", definition=definition)
    for recipient, name in zip((COORDINATOR_ID, PEER_ID), targets, strict=True):
        AgentDefinitionManager(temp_db).create(
            name,
            {
                "name": name,
                "prompts": {"agent": "Observe reports."},
                "workflows": {"rule_selectors": {"include": []}},
            },
            project_id=run.project_id,
        )
        SessionVariableManager(temp_db).set_variable(recipient, "_agent_type", name)
    await run.load(*run.vars["required_skills"])
    definitions = AgentDefinitionManager(temp_db)
    recipient_definition = definitions.get_by_name(targets[0], run.project_id)
    assert recipient_definition is not None
    if not target_enabled:
        definitions.update(recipient_definition.id, enabled=False)
        await run.send("Systems nominal | custom window", COORDINATOR_ID)
        assert run.step == "tick"
        assert run.vars["tick_window"] is None
        definitions.update(recipient_definition.id, enabled=True)
    await run.send("Systems nominal | custom window", COORDINATOR_ID)
    assert run.step == "report"
    await run.send("Systems nominal | custom window", PEER_ID)
    assert run.step == "tick"
    assert run.vars["tick_done"] is False


def _declare_report_recipient(run: SeatRun, recipient: str, name: str) -> None:
    definitions = AgentDefinitionManager(run.db)
    if definitions.get_by_name(name, run.project_id) is None:
        definitions.create(
            name,
            {
                "name": name,
                "prompts": {"agent": "Observe the monitor report."},
                "workflows": {"rule_selectors": {"include": []}},
            },
            project_id=run.project_id,
        )
    SessionVariableManager(run.db).set_variable(recipient, "_agent_type", name)


@pytest.mark.parametrize("name", ["log-monitor", "metrics-monitor"])
async def test_monitor_name_has_no_undeclared_reporting_handler(
    temp_db: HubDatabase, name: str
) -> None:
    definition = json.loads(json.dumps(_SYNTHETIC_SEATS[name]))
    definition["monitor_report_recipients"] = []
    run = SeatRun(temp_db, name, definition=definition)
    _declare_report_recipient(run, COORDINATOR_ID, "triage")
    await run.load(*run.vars["required_skills"])
    await run.send("Systems nominal | ignored window", COORDINATOR_ID)
    assert run.vars["tick_window"] is None
    assert run.step == "tick"


async def test_monitor_fanout_counts_every_delivered_recipient(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "metrics-monitor")
    _declare_report_recipient(run, COORDINATOR_ID, "triage")
    _declare_report_recipient(run, PEER_ID, "log-monitor")
    await run.load(*run.vars["required_skills"])
    window = "Systems nominal | shared fanout window"
    messages = InterSessionMessageManager(temp_db)
    message_ids = [
        messages.create_message(SESSION_ID, recipient, window).id
        for recipient in (COORDINATOR_ID, PEER_ID)
    ]
    await run.call(
        *SEND,
        {"target": "project", "content": window},
        {"success": True, "message_ids": message_ids},
    )
    assert run.vars["tick_done"] is False
    assert set(run.vars["_reported_to"]) == {"triage", "log-monitor"}
    assert set(message_ids).issubset(run.vars["_seat_consumed_messages"])
    assert run.step == "tick"


@pytest.mark.parametrize("name", ["log-monitor", "metrics-monitor"])
async def test_monitor_windows_complete_from_durable_reports(
    temp_db: HubDatabase, name: str
) -> None:
    run = SeatRun(temp_db, name)
    _declare_report_recipient(run, PEER_ID, "log-monitor")
    # Monitors report to Triage; a report to the Orchestrator completes nothing.
    SessionVariableManager(run.db).set_variable(COORDINATOR_ID, "_agent_type", "orchestrator")
    await run.load(*run.vars["required_skills"])
    await run.send("Systems nominal | sample window", COORDINATOR_ID)
    assert run.step == "tick" and run.vars["tick_window"] is None
    _declare_report_recipient(run, COORDINATOR_ID, "triage")
    for _ in range(2):
        await run.send("Systems nominal | sample window", COORDINATOR_ID)
        if name == "metrics-monitor":
            assert run.step == "report"
            await run.send("Systems nominal | a different window", PEER_ID)
            assert run.step == "report"
            assert "log-monitor" not in run.vars["_reported_to"]
            await run.send("Systems nominal | sample window", PEER_ID)
        assert run.step == "tick"
        assert run.vars["tick_done"] is False
        assert run.vars["tick_window"]


@pytest.mark.parametrize("target_form", ["uuid", "local_ref"])
async def test_metrics_monitor_window_needs_both_delivered_recipients(
    temp_db: HubDatabase, target_form: str
) -> None:
    run = SeatRun(temp_db, "metrics-monitor")
    _declare_report_recipient(run, COORDINATOR_ID, "triage")
    _declare_report_recipient(run, PEER_ID, "log-monitor")
    await run.load(*run.vars["required_skills"])
    window = "Systems nominal | sample window"

    def target(recipient: str) -> str:
        # The engine reads the stored recipient, so the argument form must not matter.
        if target_form == "uuid":
            return recipient
        session = run.sessions.get(recipient)
        assert session is not None
        return f"#{session.seq_num}"

    async def deliver(recipient: str, *, succeeded: bool = True) -> None:
        message = InterSessionMessageManager(run.db).create_message(SESSION_ID, recipient, window)
        await run.call(
            *SEND,
            {"target": "session", "target_id": target(recipient), "content": window},
            {"success": succeeded, "message_ids": [message.id]},
        )

    # A failed triage delivery opens no window.
    await deliver(COORDINATOR_ID, succeeded=False)
    assert run.step == "tick"
    assert run.vars["tick_window"] is None
    # A delivered triage report opens the window and still waits on the log monitor.
    await deliver(COORDINATOR_ID)
    assert run.step == "report"
    assert "triage" in run.vars["_reported_to"]
    # A failed log-monitor delivery and a non-recipient target leave the window open.
    await deliver(PEER_ID, succeeded=False)
    await deliver(OWNER_ID)
    assert run.step == "report"
    assert "log-monitor" not in run.vars["_reported_to"]
    # Only a delivered log-monitor report completes the window.
    await deliver(PEER_ID)
    assert run.step == "tick"
    assert run.vars["tick_done"] is False


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


@pytest.mark.parametrize("step", ["review", "stamp"])
@pytest.mark.parametrize("name,value", [("consensus", True), ("consensus_sha", SHA)])
async def test_plan_adversary_consensus_variables_are_handler_owned(
    temp_db: HubDatabase, step: str, name: str, value: object
) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    if step == "stamp":
        await run.send(f"EVENT=CONSENSUS SHA={SHA}")
        # Only the handler's own write lands: the consensus pair holds the handler-written SHA.
        assert run.vars["consensus"] is True
        assert run.vars["consensus_sha"] == SHA
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
        f"Evidence follows.\nEVENT=CONSENSUS SHA={SHA}",
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


@pytest.mark.parametrize("suffix", ["\tNOTE=approved", "\r\nPLAN=example"])
async def test_plan_adversary_consensus_preserves_whitespace_delimiters(
    temp_db: HubDatabase, suffix: str
) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    await run.send(f"EVENT=CONSENSUS SHA={SHA}{suffix}")
    assert run.step == "stamp"
    assert run.vars["consensus"] is True
    assert run.vars["consensus_sha"] == SHA


@pytest.mark.parametrize("suffix", ["\tNOTE=unresolved", "\r\nPLAN=example"])
async def test_plan_adversary_withdrawal_preserves_whitespace_delimiters(
    temp_db: HubDatabase, suffix: str
) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    await run.send(f"EVENT=CONSENSUS SHA={SHA}")
    assert run.step == "stamp"
    await run.send(f"EVENT=CONSENSUS_WITHDRAWN SHA={SHA}{suffix}")
    assert run.step == "review"
    assert run.vars["consensus"] is False


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
    assert run.step == "route_skills"
    assert run.vars["assigned_task_ref"] == ref


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
async def test_candidate_looking_text_is_retrieved_without_protocol_effects(
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
    assert result["success"] is True
    assert result["message"]["content"] == message.content
    assert "candidate_binding_error" not in result
    await run.call("gobby-agents", "get_inter_session_message", {"message_id": message.id}, result)
    assert run.step == "await"
    assert run.vars["candidate_task"] is None


async def test_seat_consumed_messages_keeps_last_64(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    await run.load(*run.vars["required_skills"])
    message_ids = []
    for index in range(70):
        event = "CONSENSUS" if index % 2 == 0 else "CONSENSUS_WITHDRAWN"
        message = InterSessionMessageManager(temp_db).create_message(
            SESSION_ID, COORDINATOR_ID, f"EVENT={event} SHA={SHA} NOTE=bounded"
        )
        message_ids.append(message.id)
        await run.call(
            *SEND,
            {"target": "session", "target_id": COORDINATOR_ID, "content": message.content},
            {"success": True, "message_ids": [message.id]},
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
    await_step = next(step for step in body["steps"] if step["name"] == "await")
    await_step["on_mcp_success"].append(
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
    definition = json.loads(json.dumps(_SYNTHETIC_SEATS["plan-adversary"]))
    if not transition:
        definition["step_workflow"]["steps"][0]["transitions"] = []
    run = SeatRun(temp_db, "plan-adversary", definition=definition)
    await run.load(*run.vars["required_skills"])

    def stale(*args: Any, **kwargs: Any) -> None:
        raise StaleStepInstanceWriteError("concurrent seat completion")

    monkeypatch.setattr(AgentStepInstanceManager, "save", stale)
    await run.send(f"EVENT=CONSENSUS SHA={SHA} NOTE=approved")
    assert run.step == "review"
    assert "Stale step completion write" in caplog.text


async def test_stale_consensus_transition_keeps_its_message_replayable(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = SeatRun(temp_db, "plan-adversary")
    save = AgentStepInstanceManager.save
    lost: list[str] = []

    def lose_first_transition(
        self: AgentStepInstanceManager,
        instance: AgentStepInstance,
        *,
        if_match: tuple[str, datetime] | None = None,
    ) -> None:
        if instance.current_step == "stamp" and not lost:
            lost.append(instance.id)
            raise StaleStepInstanceWriteError("a concurrent write won the step instance")
        save(self, instance, if_match=if_match)

    monkeypatch.setattr(AgentStepInstanceManager, "save", lose_first_transition)
    message = InterSessionMessageManager(temp_db).create_message(
        SESSION_ID, COORDINATOR_ID, f"EVENT=CONSENSUS SHA={SHA} NOTE=approved"
    )
    arguments = {"target": "session", "target_id": COORDINATOR_ID, "content": message.content}
    output = {"success": True, "message_ids": [message.id]}
    await run.call(*SEND, arguments, output)
    assert lost
    assert run.step == "review"
    assert message.id not in run.vars.get("_seat_consumed_messages", [])
    await run.call(*SEND, arguments, output)
    assert run.step == "stamp"
    assert run.vars["consensus_sha"] == SHA
    assert run.vars["_seat_consumed_messages"] == [message.id]
    await run.call(*SEND, arguments, output)
    assert run.step == "stamp"
    assert run.vars["_seat_consumed_messages"] == [message.id]
