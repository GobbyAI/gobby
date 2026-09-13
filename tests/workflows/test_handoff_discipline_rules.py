"""Handoff teaching, model-aware warning loads, and context-limit recovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.config.sessions import ContextHandoffConfig
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.observer_context_usage import (
    _thresholds,
    detect_context_compact_guidance,
    detect_mid_turn_context_compact_guidance,
)
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit
SESSION_ID = "11111111-1111-4111-8111-111111111111"
SKILL = "gobby:references/sessions/handoffs.md"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    return temp_db


def _tool_event(
    server: str, tool: str, arguments: dict[str, Any], shape: str = "wrapper"
) -> HookEvent:
    if shape == "direct":
        data: dict[str, Any] = {
            "tool_name": f"mcp__{server}__{tool}",
            "tool_input": arguments,
        }
    else:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": server,
                "tool_name": tool,
                "arguments": json.dumps(arguments) if shape == "json" else arguments,
            },
        }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["wrapper", "direct", "json"])
async def test_handoff_requires_feedback_even_with_skill_loaded(
    db: HubDatabase, shape: str
) -> None:
    engine = RuleEngine(db)
    event = _tool_event("gobby-sessions", "set_handoff", {"clear_session": False}, shape)
    variables: dict[str, Any] = {
        "loaded_skill_references": [SKILL],
        "project": {"name": "gobby"},
    }
    blocked = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert blocked.decision == "block"
    assert "gobby-sessions:feedback" in (blocked.reason or "")
    variables["_gobby_feedback_epoch_submitted"] = True
    allowed = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert allowed.decision != "block"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["get_tool_schema", "mcp__gobby__get_tool_schema"])
async def test_schema_discovery_requires_feedback_then_handoff_skill(
    db: HubDatabase, tool_name: str
) -> None:
    event = _tool_event("gobby-sessions", "get_handoff", {})
    event.data = {
        "tool_name": tool_name,
        "tool_input": {"server_name": "gobby-sessions", "tool_name": "set_handoff"},
    }
    normalize_tool_fields(event.data)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {"project": {"name": "gobby"}}
    feedback_block = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert feedback_block.decision == "block"
    assert "gobby-sessions:feedback" in (feedback_block.reason or "")
    assert "10,000" in (feedback_block.reason or "")
    variables["_gobby_feedback_epoch_submitted"] = True
    skill_block = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert skill_block.decision == "block"
    assert skill_fetch_directive(SKILL) in (skill_block.reason or "")
    variables["loaded_skill_references"] = [SKILL]
    allowed = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert allowed.decision != "block"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["wrapper", "direct", "json"])
async def test_clear_requires_closed_tasks(db: HubDatabase, shape: str) -> None:
    engine = RuleEngine(db)
    variables: dict[str, Any] = {
        "loaded_skill_references": [SKILL],
        "claimed_tasks": {"task": "#1"},
    }
    event = _tool_event("gobby-sessions", "set_handoff", {"clear_session": True}, shape)
    blocked = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert blocked.decision == "block"
    assert "clear_session=false" in (blocked.reason or "")
    compact = _tool_event("gobby-sessions", "set_handoff", {"clear_session": False}, shape)
    allowed = await engine.evaluate(compact, session_id=SESSION_ID, variables=variables)
    assert allowed.decision != "block"
    variables["claimed_tasks"] = {}
    allowed = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert allowed.decision != "block"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server", "tool"),
    [("gobby-sessions", "feedback"), ("gobby-sessions", "get_handoff"), ("other", "set_handoff")],
)
async def test_handoff_schema_gate_leaves_prerequisites_available(
    db: HubDatabase, server: str, tool: str
) -> None:
    event = _tool_event("gobby-sessions", "get_handoff", {})
    event.data = {
        "tool_name": "mcp__gobby__get_tool_schema",
        "tool_input": {"server_name": server, "tool_name": tool},
    }
    normalize_tool_fields(event.data)
    result = await RuleEngine(db).evaluate(
        event, session_id=SESSION_ID, variables={"project": {"name": "gobby"}}
    )
    assert result.decision != "block"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["wrapper", "direct", "json"])
@pytest.mark.parametrize(
    "server,tool",
    [("gobby-sessions", "set_handoff"), ("gobby-agents", "end_agent_run")],
)
async def test_authoring_requires_loaded_skill(
    db: HubDatabase, shape: str, server: str, tool: str
) -> None:
    event = _tool_event(server, tool, {"current_state": "Ready", "next_steps": ["Verify"]}, shape)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {"loaded_skills": ["tasks"]}

    blocked = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert blocked.decision == "block"
    assert skill_fetch_directive(SKILL) in (blocked.reason or "")
    assert "cumulative history" in (blocked.reason or "")

    variables["loaded_skill_references"] = ["gobby:references/tasks/overview.md", SKILL]
    allowed = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert allowed.decision != "block"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["wrapper", "direct", "json"])
async def test_handoff_retrieval_remains_available(db: HubDatabase, shape: str) -> None:
    response = await RuleEngine(db).evaluate(
        _tool_event("gobby-sessions", "get_handoff", {}, shape),
        session_id=SESSION_ID,
        variables={"loaded_skills": [], "handoff_pull_pending": True},
    )
    assert response.decision != "block"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pending,disabled,expected", [(False, False, True), (True, False, False), (False, True, False)]
)
async def test_startup_restraint_respects_pull_order_and_explicit_opt_out(
    db: HubDatabase, pending: bool, disabled: bool, expected: bool
) -> None:
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
    )
    response = await RuleEngine(db).evaluate(
        event,
        session_id=SESSION_ID,
        variables={
            "loaded_skills": ["brevity"],
            "loaded_skill_references": [
                "gobby:references/skills/loading.md",
                "gobby:references/memory/overview.md",
            ],
            "handoff_pull_pending": pending,
            "restraint_disabled": disabled,
            "servers_listed": True,
        },
    )
    assert (skill_fetch_directive("restraint") in (response.context or "")) is expected


@dataclass
class _Sessions:
    context_used_tokens: int
    context_window: int | None

    def get(self, session_id: str) -> _Sessions:
        return self


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", [HookEventType.BEFORE_AGENT, HookEventType.AFTER_TOOL])
@pytest.mark.parametrize(
    "window,config",
    [
        (128_000, None),
        (200_000, None),
        (256_000, None),
        (None, None),
        (1_000_000, ContextHandoffConfig(warn_tokens=200_000)),
    ],
)
async def test_warning_loads_follow_model_threshold_and_loaded_state(
    db: HubDatabase,
    event_type: HookEventType,
    window: int | None,
    config: ContextHandoffConfig | None,
) -> None:
    event = HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
    )
    variables: dict[str, Any] = {
        "loaded_skills": ["brevity", "restraint"],
        "loaded_skill_references": [
            "gobby:references/skills/loading.md",
            "gobby:references/memory/overview.md",
        ],
        "servers_listed": True,
    }
    engine = RuleEngine(db)
    warning, _block, _cadence = _thresholds(config, window)
    for used, expected in [(warning - 1, False), (warning, True)]:
        manager = _Sessions(used, window)
        if event_type == HookEventType.BEFORE_AGENT:
            detect_context_compact_guidance(variables, SESSION_ID, manager, config)
        else:
            detect_mid_turn_context_compact_guidance(event, variables, SESSION_ID, manager, config)
        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert (skill_fetch_directive(SKILL) in (response.context or "")) is expected

    variables["loaded_skill_references"].append(SKILL)
    loaded = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert skill_fetch_directive(SKILL) not in (loaded.context or "")

    variables["loaded_skill_references"] = []
    variables["handoff_pull_pending"] = True
    pending = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
    assert skill_fetch_directive(SKILL) not in (pending.context or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["wrapper", "direct", "json"])
@pytest.mark.parametrize(
    "arguments,allowed",
    [
        ({"name": "gobby", "path": "references/sessions/handoffs.md"}, True),
        ({"cursor": "next-page"}, True),
        ({"name": "python"}, False),
    ],
)
async def test_context_limit_allows_handoff_skill_and_continuation(
    db: HubDatabase, shape: str, arguments: dict[str, Any], allowed: bool
) -> None:
    response = await RuleEngine(db).evaluate(
        _tool_event("gobby-skills", "get_skill_file", arguments, shape),
        session_id=SESSION_ID,
        variables={
            "context_compact_mid_turn_pressure_band": "block",
            "context_compact_block_message": "Context limit reached",
        },
    )
    assert (response.decision != "block") is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["compact", "clear"])
async def test_context_reset_rearms_authoring_gate(db: HubDatabase, boundary: str) -> None:
    engine = RuleEngine(db)
    variables: dict[str, Any] = {"loaded_skill_references": [SKILL]}
    reset = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"source": boundary},
    )
    await engine.evaluate(reset, session_id=SESSION_ID, variables=variables)
    response = await engine.evaluate(
        _tool_event("gobby-sessions", "set_handoff", {}),
        session_id=SESSION_ID,
        variables=variables,
    )
    assert response.decision == "block"
    assert skill_fetch_directive(SKILL) in (response.reason or "")
