"""The end-of-run gates hold end_agent_run until feedback and the handoff reference load."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.hooks.session_activation import (
    _resolve_active_rule_names,
    clear_active_rule_names_cache,
)
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.integration

HANDOFF_SKILL = "gobby:references/sessions/handoffs.md"
END_OF_RUN_GATES = {"require-handoff-feedback-before-authoring", "require-handoff-discipline"}
END_RUN_ARGUMENTS = {
    "current_state": "Work for #7 is committed and the task is closed.",
    "next_steps": ["Continue with the next ready task."],
}


def _seat_json(name: str) -> str:
    selectors = [f"name:{rule}" for rule in sorted(END_OF_RUN_GATES)]
    return json.dumps(
        {
            "name": name,
            "prompts": {"agent": "Run the assigned task."},
            "workflows": {"rule_selectors": {"include": selectors, "exclude": []}},
        }
    )


@pytest.fixture
def db(hub_db: HubDatabase) -> Iterator[HubDatabase]:
    """Seed synthetic seats that select the gates, plus the rules the engine reads."""
    clear_active_rule_names_cache()
    agents = AgentDefinitionManager(hub_db)
    for seat in ("developer", "code-reviewer"):
        agents.create(name=seat, source="custom", definition_json=_seat_json(seat))
    sync_bundled_rules(hub_db, get_bundled_rules_path())
    yield hub_db
    clear_active_rule_names_cache()


@pytest.fixture
def session_id(db: HubDatabase) -> str:
    session, project = str(uuid4()), str(uuid4())
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (project, "gobby"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (session, session, "21000000-0000-4000-8000-000000000001", "claude", project),
    )
    return session


def _end_run(session_id: str) -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": "gobby-agents",
            "tool_name": "end_agent_run",
            "arguments": END_RUN_ARGUMENTS,
        },
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", [None, "developer", "code-reviewer"])
async def test_end_of_run_gates_hold_end_agent_run(
    db: HubDatabase, session_id: str, agent_type: str | None
) -> None:
    if agent_type is not None:
        active = _resolve_active_rule_names(db, agent_type, None)
        assert active is not None
        assert END_OF_RUN_GATES <= active
    engine = RuleEngine(db)
    variables: dict[str, Any] = {
        "is_spawned_agent": agent_type is not None,
        "project": {"name": "gobby"},
        "loaded_skill_references": [],
    }
    if agent_type is not None:
        variables["_agent_type"] = agent_type
    end_run = _end_run(session_id)

    async def run(event: HookEvent) -> HookResponse:
        return await engine.evaluate(event, session_id=session_id, variables=variables)

    survey = await run(end_run)
    assert survey.decision == "block"
    assert "gobby-sessions:feedback" in (survey.reason or "")

    variables["_gobby_feedback_epoch_submitted"] = True
    guidance = await run(end_run)
    assert guidance.decision == "block"
    assert skill_fetch_directive(HANDOFF_SKILL).splitlines()[0] in (guidance.reason or "")

    variables["loaded_skill_references"] = [HANDOFF_SKILL]
    allowed = await run(end_run)
    assert allowed.decision != "block", allowed.reason
