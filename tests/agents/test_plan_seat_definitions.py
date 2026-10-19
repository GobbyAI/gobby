"""Contract tests for the planning runbook's live seat definitions.

The planning runbook spawns plan-writer, plan-enhancer and plan-adversary as
live seats. Each definition carries its own seat role.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path, sync_bundled_agents
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.agents._yaml_helpers import _field, flat

pytestmark = pytest.mark.unit

SEATS = ("plan-writer", "plan-enhancer", "plan-adversary")

HANDOFF_TOOLS = {
    "gobby-plans:derive_plan_handoff_manifest",
    "gobby-plans:apply_plan_handoff_manifest",
}


async def test_adversary_stamps_only_after_consensus(temp_db: HubDatabase) -> None:
    body = _load("plan-adversary")
    workflow = body.step_workflow
    assert workflow is not None
    steps = {step.name: step for step in workflow.steps}
    assert set(steps) == {"load_skill", "review", "stamp"}
    for name in ("load_skill", "review"):
        assert HANDOFF_TOOLS <= set(steps[name].blocked_mcp_tools or [])
    stamp = steps["stamp"]
    assert not HANDOFF_TOOLS.intersection(body.blocked_mcp_tools or [])
    assert not HANDOFF_TOOLS.intersection(stamp.blocked_mcp_tools or [])
    assert stamp.allowed_mcp_tools == "all" or HANDOFF_TOOLS <= set(stamp.allowed_mcp_tools or [])
    review = steps["review"]
    assert [(entry.to, entry.when) for entry in review.transitions] == [
        ("stamp", "vars.consensus and not vars.bounced")
    ]
    assert [(entry.to, entry.when) for entry in stamp.transitions] == [("review", "vars.bounced")]
    from tests.workflows.test_seat_step_loops import DERIVE, SHA, SeatRun

    run = SeatRun(temp_db, "plan-adversary")
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
    assert workflow.variables["consensus"] is False
    assert workflow.variables["bounced"] is False
    assert workflow.exit_condition == "vars.seat_complete"
    assert any(
        _field(entry, "tool") == "end_agent_run"
        and _field(entry, "variable") == "seat_complete"
        and _field(entry, "value") is True
        for entry in (stamp.on_mcp_success or [])
    )


def test_adversary_rejects_open_questions_before_stamping() -> None:
    body = _load("plan-adversary")
    prompts = body.prompts
    workflow = body.step_workflow
    assert prompts is not None
    assert workflow is not None
    steps = {step.name: step for step in workflow.steps}
    for text in (
        prompts.persona,
        prompts.agent,
        steps["review"].status_message,
        steps["stamp"].status_message,
    ):
        instructions = flat(text or "")
        assert "Reject any plan with unresolved questions" in instructions
        assert "do not stamp, approve, or expand it" in instructions

    instructions = flat(prompts.agent or "")
    for unresolved in (
        "Open Questions section",
        "TBD",
        "to-decide",
        "unchosen either/or",
        "choice left to the implementer",
    ):
        assert unresolved in instructions
    assert "Bounce the plan to the writer" in instructions
    assert "each open question to the operator" in instructions
    assert "options and a recommendation" in instructions
    assert "resolves every question in the plan text" in instructions
    assert "re-review the corrected plan before consensus or stamping" in instructions


def test_adversary_blocks_all_evidence_round_tools() -> None:
    tools = {
        "prepare_plan_review_round",
        "get_plan_review_snapshot",
        "bind_evidence_run",
        "derive_plan_review_manifest",
        "validate_plan_review_coverage",
        "append_plan_changelog_round",
        "finalize_plan_review_evidence",
        "apply_plan_review_manifest",
        "apply_plan_review_repairs",
        "checkpoint_plan_review_lesson_mint",
    }
    assert {f"gobby-plans:{name}" for name in tools} <= set(
        _load("plan-adversary").blocked_mcp_tools or []
    )


@pytest.mark.parametrize("name", ("plan-writer", "plan-adversary"))
def test_planning_seats_require_coverage_skill(name: str) -> None:
    workflow = _load(name).step_workflow
    assert workflow is not None
    assert "gobby:references/plan/coverage.md" in workflow.variables["required_skills"]
    load = next(step for step in workflow.steps if step.name == "load_skill")
    assert "gobby:references/plan/coverage.md" in (load.status_message or "")
    assert [entry.when for entry in load.transitions] == [
        "all(skill_loaded(skill) for skill in vars.required_skills)"
    ]


def test_writer_load_step_takes_messages_before_skills_load() -> None:
    workflow = _load("plan-writer").step_workflow
    assert workflow is not None
    load = next(step for step in workflow.steps if step.name == "load_skill")
    assert "gobby-agents:send_message" in (load.allowed_mcp_tools or [])


@pytest.mark.parametrize("name", SEATS)
def test_planning_seats_select_the_no_send_keys_rule(name: str) -> None:
    selectors = _load(name).workflows.rule_selectors
    assert selectors is not None
    assert "name:no-send-keys" in selectors.include


def _load(name: str) -> AgentDefinitionBody:
    path = get_bundled_agents_path() / f"{name}.yaml"
    return AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text()))


def _instructions(body: AgentDefinitionBody) -> str:
    prompts = body.prompts
    return flat(f"{prompts.persona if prompts else ''} {prompts.agent if prompts else ''}")


def _parse_row(payload: object) -> AgentDefinitionBody:
    if isinstance(payload, str):
        return AgentDefinitionBody.model_validate_json(payload)
    return AgentDefinitionBody.model_validate(payload)


def test_seat_definitions_sync_and_validate(definition_db: PostgresHubDatabase) -> None:
    result = sync_bundled_agents(definition_db)

    assert result["success"] is True
    assert result["errors"] == []
    manager = AgentDefinitionManager(definition_db)
    for name in SEATS:
        row = manager.get_by_name(name)
        assert row is not None, name
        assert row.enabled is True, name
        assert _parse_row(row.definition_json) == _load(name), name
    adversary = _instructions(_load("plan-adversary"))
    assert "plan-writer" in adversary
    assert "approve_review" not in adversary


def test_seat_instructions_carry_seat_role() -> None:
    writer = _instructions(_load("plan-writer"))
    enhancer = _instructions(_load("plan-enhancer"))
    adversary = _instructions(_load("plan-adversary"))

    assert "You are the Plan Writer seat" in writer
    assert "draft" in writer
    assert "gobby:references/plan/coverage.md" in writer
    assert "You are the Plan Enhancer seat" in enhancer
    assert "You are the Plan Adversary seat" in adversary
    for text in (enhancer, adversary):
        assert "send_message" in text
        assert "plan-writer" in text
    assert "consensus" in adversary
    assert "## M1 Task Manifest" in adversary


def test_writer_spawns_no_enhancer_enhancer_stays_live() -> None:
    writer = _load("plan-writer")
    enhancer = _load("plan-enhancer")

    assert "gobby-agents:spawn_agent" in (writer.blocked_mcp_tools or [])
    assert "never spawn" in _instructions(writer)
    steps = enhancer.step_workflow.steps if enhancer.step_workflow else []
    send_latches = [
        entry
        for step in steps
        for entry in (getattr(step, "on_mcp_success", None) or [])
        if _field(entry, "tool") == "send_message"
    ]
    assert send_latches == []
    assert "stay live" in _instructions(enhancer)


@pytest.mark.parametrize(
    ("name", "mode"),
    [
        ("plan-writer", "interactive"),
        ("plan-adversary", "interactive"),
        ("plan-enhancer", "one_shot"),
    ],
)
def test_planning_run_lifetime(name: str, mode: str) -> None:
    body = _load(name)
    assert body.execution_mode == mode
    assert body.idle_ttl_seconds is None


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

    for name in SEATS:
        assert Path(get_bundled_agents_path() / f"{name}.yaml").is_file(), name
        response = await engine.evaluate(
            _spawn_event(pipeline_child.id, name),
            session_id=pipeline_child.id,
            variables=dict(pipeline_variables),
        )
        assert response.decision == "allow", (name, response.reason)
