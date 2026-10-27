"""Behavior tests for the planning seats.

The plan-adversary stamps a plan only after consensus, a pipeline child may spawn the
planning seats, and a seat's run lifetime follows its definition. The seats are synthetic:
step graphs are built inline or come from the seat-loop driver, and no test reads a bundled
definition. Each inline seat carries only the transition or admission under test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.spawn_agent._run_lifetime import resolve_run_lifetime
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.fixtures.agent_definitions import seed_seat
from tests.workflows.test_seat_step_loops import DERIVE, SHA, SeatRun

pytestmark = pytest.mark.unit

# The planning seats a pipeline child spawns, seeded as synthetic registry rows.
SYNTHETIC_PLANNING_SEATS = ("synthetic-writer", "synthetic-enhancer", "synthetic-adversary")

DERIVE_TOOL = "gobby-plans:derive_plan_handoff_manifest"
EVIDENCE_TOOLS = (
    "gobby-plans:prepare_plan_review_round",
    "gobby-plans:finalize_plan_review_evidence",
)


def _protocol(tool: str) -> dict[str, str]:
    """Route one successful call through the engine's seat protocol handler."""
    return {"server": "gobby-agents", "tool": tool, "action": "advance_seat"}


# A synthetic adversary: the consensus transition and the derive gate are the only
# transitions under test. The derive tool is blocked until consensus moves the seat.
CONSENSUS_ADVERSARY: dict[str, Any] = {
    "name": "plan-adversary",
    "prompts": {"agent": "Synthetic consensus-gated adversary seat."},
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
                "blocked_mcp_tools": [DERIVE_TOOL],
                "on_mcp_success": [_protocol("send_message")],
                "transitions": [{"to": "stamp", "when": "vars.consensus and not vars.bounced"}],
            },
            {
                "name": "stamp",
                "on_mcp_success": [_protocol("send_message")],
                "transitions": [{"to": "review", "when": "vars.bounced"}],
            },
        ],
    },
}

# A synthetic adversary whose review step refuses the evidence-round tools.
EVIDENCE_BLOCKING_ADVERSARY: dict[str, Any] = {
    "name": "plan-adversary",
    "prompts": {"agent": "Synthetic evidence-blocking adversary seat."},
    "workflows": {"rule_selectors": {"include": [], "exclude": []}},
    "step_workflow": {
        "variables": {},
        "steps": [{"name": "review", "blocked_mcp_tools": list(EVIDENCE_TOOLS)}],
    },
}

# A synthetic writer: the load step takes messages and refuses spawns, and it moves on
# only once its required skill is loaded.
WRITER_LOAD_GATE: dict[str, Any] = {
    "name": "plan-writer",
    "prompts": {"agent": "Synthetic writer seat."},
    "workflows": {"rule_selectors": {"include": [], "exclude": []}},
    "step_workflow": {
        "variables": {"required_skills": ["gobby:references/synthetic/coverage.md"]},
        "steps": [
            {
                "name": "load_skill",
                "allowed_mcp_tools": [
                    "gobby-agents:send_message",
                    "gobby-skills:get_skill",
                    "gobby-skills:get_skill_file",
                ],
                "blocked_mcp_tools": ["gobby-agents:spawn_agent"],
                "transitions": [
                    {
                        "to": "review",
                        "when": "all(skill_loaded(skill) for skill in vars.required_skills)",
                    }
                ],
            },
            {"name": "review"},
        ],
    },
}


async def test_adversary_stamps_only_after_consensus(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "plan-adversary", definition=CONSENSUS_ADVERSARY)
    await run.load(*run.vars["required_skills"])
    assert run.step == "review"
    assert not await run.admits(*DERIVE)
    await run.send("EVENT=CONSENSUS SHA=abc NOTE=unresolved")
    assert run.step == "review"
    assert run.vars["consensus"] is False
    await run.send(f"EVENT=CONSENSUS SHA={SHA} NOTE=approved")
    assert run.step == "stamp"
    assert run.vars["consensus"] is True
    assert run.vars["consensus_sha"] == SHA
    assert await run.admits(*DERIVE)


async def test_adversary_refuses_the_evidence_round_tools(temp_db: HubDatabase) -> None:
    run = SeatRun(temp_db, "plan-adversary", definition=EVIDENCE_BLOCKING_ADVERSARY)
    for qualified in EVIDENCE_TOOLS:
        server, tool = qualified.split(":", 1)
        assert not await run.admits(server, tool), qualified
    assert await run.admits("gobby-agents", "send_message")


async def test_writer_takes_messages_and_refuses_spawns_until_coverage_loads(
    temp_db: HubDatabase,
) -> None:
    run = SeatRun(temp_db, "plan-writer", definition=WRITER_LOAD_GATE)
    assert await run.admits("gobby-agents", "send_message")
    assert not await run.admits("gobby-agents", "spawn_agent")
    assert run.step == "load_skill"
    await run.load(*run.vars["required_skills"])
    assert run.step == "review"


@pytest.mark.parametrize(
    ("body_mode", "override", "resumed", "expected"),
    [
        ("one_shot", None, False, "one_shot"),
        ("interactive", None, False, "interactive"),
        ("one_shot", "interactive", False, "interactive"),
        ("one_shot", None, True, "interactive"),
    ],
)
def test_definition_owns_the_run_lifetime_unless_the_spawn_overrides_it(
    body_mode: Literal["one_shot", "interactive"],
    override: Literal["one_shot", "interactive"] | None,
    resumed: bool,
    expected: str,
) -> None:
    fields: dict[str, Any] = {
        "name": "lifetime-seat",
        "prompts": {"agent": "Synthetic lifetime seat."},
        "workflows": {"rule_selectors": {"include": [], "exclude": []}},
        "execution_mode": body_mode,
    }
    if body_mode == "interactive":
        fields["idle_ttl_seconds"] = 900
    body = AgentDefinitionBody.model_validate(fields)
    lifetime = resolve_run_lifetime(body, override, resumed=resumed)
    assert lifetime.execution_mode == expected


def _spawn_event(caller_id: str, agent: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=caller_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-agents",
                "tool_name": "spawn_agent",
                "arguments": {"agent": agent, "checkout_mode": "none"},
            },
        },
        metadata={"_platform_session_id": caller_id},
    )


@pytest.mark.asyncio
async def test_seat_spawns_admitted_from_pipeline_child(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    for name in SYNTHETIC_PLANNING_SEATS:
        seed_seat(temp_db, name)
    engine = RuleEngine(temp_db, session_manager=session_manager)
    project_id = str(sample_project["id"])
    caller = session_manager.register(
        external_id="plan-seat-runbook-caller",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
    )
    # The session PipelineExecutor registers for a top-level pipeline run.
    pipeline_child = session_manager.register(
        external_id="pipeline-plan-seat-runbook",
        machine_id=None,
        source="pipeline",
        project_id=project_id,
        parent_session_id=caller.id,
        agent_depth=0,
    )
    pipeline_variables: dict[str, Any] = {"_agent_type": "pipeline"}

    for name in SYNTHETIC_PLANNING_SEATS:
        response = await engine.evaluate(
            _spawn_event(pipeline_child.id, name),
            session_id=pipeline_child.id,
            variables=dict(pipeline_variables),
        )
        assert response.decision == "allow", (name, response.reason)
