"""Tests for block-update-task: an agent definition grants task edits by excluding it."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.hooks.session_activation import (
    _resolve_active_rule_names,
    clear_active_rule_names_cache,
)
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE = "block-update-task"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
TASK_UUID = "44444444-4444-4444-8444-444444444444"
TASK_REF = "#7"
MARKER_BLOCK = (
    "<!-- gobby:discovery-stage:ideation:start -->\n"
    "## Discovery Brief\n"
    "<!-- gobby:discovery-stage:ideation:end -->"
)

TASK_EDITORS = ("orchestrator", "assistant", "analyst", "architect", "product-manager")
BLOCKED_DEFINITIONS = ("default", "developer", "code-reviewer", "researcher", "lane-manager")


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    """Sync the bundled agents and rules, the inputs session activation reads."""
    clear_active_rule_names_cache()
    assert sync_bundled_agents(temp_db)["success"] is True
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    yield temp_db
    clear_active_rule_names_cache()


def _only_rule_under_test_enabled(db: HubDatabase) -> None:
    manager = RuleDefinitionManager(db)
    for row in manager.list_all():
        if row.name != RULE and row.enabled:
            manager.update(row.id, enabled=False)


def _active_rules(db: HubDatabase, agent_name: str) -> set[str]:
    active = _resolve_active_rule_names(db, agent_name, None)
    assert active is not None, agent_name
    return active


def _tasks_call(tool_name: str, arguments: dict[str, Any]) -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": "gobby-tasks",
            "tool_name": tool_name,
            "arguments": arguments,
        },
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
    )


@pytest.fixture(params=["_agent_type", "_active_rule_names"])
def activated_by(request: pytest.FixtureRequest) -> str:
    """RuleEngine filters by the agent definition when set, else by the activated names."""
    return str(request.param)


async def _decision(
    db: HubDatabase, agent_name: str, event: HookEvent, activated_by: str
) -> tuple[str, str]:
    """Evaluate one call as a session activated from `agent_name`."""
    variables: dict[str, Any] = {"claimed_tasks": {TASK_UUID: TASK_REF}}
    if activated_by == "_agent_type":
        variables["_agent_type"] = agent_name
    else:
        variables["_active_rule_names"] = sorted(_active_rules(db, agent_name))
    _only_rule_under_test_enabled(db)
    response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables=variables)
    return response.decision, response.reason or ""


@pytest.mark.parametrize("agent_name", TASK_EDITORS)
def test_task_editor_definitions_exclude_the_rule(db: HubDatabase, agent_name: str) -> None:
    assert RULE not in _active_rules(db, agent_name)


@pytest.mark.parametrize("agent_name", BLOCKED_DEFINITIONS)
def test_other_definitions_activate_the_rule(db: HubDatabase, agent_name: str) -> None:
    assert RULE in _active_rules(db, agent_name)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", BLOCKED_DEFINITIONS)
async def test_owner_cannot_edit_its_own_claimed_task(
    db: HubDatabase, agent_name: str, activated_by: str
) -> None:
    decision, reason = await _decision(
        db,
        agent_name,
        _tasks_call("update_task", {"task_id": TASK_REF, "title": "Retitled"}),
        activated_by,
    )

    assert decision == "block"
    assert RULE in reason
    assert "Orchestrator" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", ("orchestrator", "assistant"))
async def test_coordinators_edit_another_sessions_claimed_task(
    db: HubDatabase, agent_name: str, activated_by: str
) -> None:
    decision, _ = await _decision(
        db, agent_name, _tasks_call("update_task", {"task_id": "#8", "priority": 1}), activated_by
    )

    assert decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", ("analyst", "architect", "product-manager"))
async def test_discovery_definitions_write_their_marker_block(
    db: HubDatabase, agent_name: str, activated_by: str
) -> None:
    decision, _ = await _decision(
        db,
        agent_name,
        _tasks_call("update_task", {"task_id": TASK_REF, "description": MARKER_BLOCK}),
        activated_by,
    )

    assert decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ("claim_task", "close_task", "get_task"))
async def test_rule_leaves_other_task_tools_to_their_own_guards(
    db: HubDatabase, tool_name: str, activated_by: str
) -> None:
    decision, _ = await _decision(
        db, "developer", _tasks_call(tool_name, {"task_id": TASK_REF}), activated_by
    )

    assert decision == "allow"
