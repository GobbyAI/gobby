"""Contract tests for the planning runbook's live seat definitions.

The planning runbook spawns plan-writer, plan-enhancer and plan-adversary as
live seats. Each definition carries its own seat role; none points the seat at
a `.gobby/roles` file or `_common.md`.
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
    assert "You are the Plan Enhancer seat" in enhancer
    assert "You are the Plan Adversary seat" in adversary
    for text in (enhancer, adversary):
        assert "send_message" in text
        assert "plan-writer" in text
    assert "consensus" in adversary
    assert "## M1 Task Manifest" in adversary
    for name, text in zip(SEATS, (writer, enhancer, adversary), strict=True):
        assert ".gobby/roles" not in text, name
        assert "_common.md" not in text, name


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
        metadata={"_platform_session_id": caller_id, "_mcp_proxy_dispatch": True},
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
