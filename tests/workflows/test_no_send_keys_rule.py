"""The opt-in ``no-send-keys`` rule gates terminal control for spawned agents.

``send_keys`` and ``capture_output`` are operator tools: they bypass step and
agent MCP allow-lists. Spawned Gobby agents are no longer refused by enforcement
itself, so the only thing that withholds them is this rule, selected per agent
definition through ``workflows.rule_selectors``. These tests run the real rule
engine over the synced bundled rules to prove the bypass does not skip it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentSelector
from gobby.workflows.engine.block_batching import operator_event_is_subagent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.fixtures.agent_definitions import make_agent_definition, make_agent_workflows

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
TERMINAL_TOOLS = ("send_keys", "capture_output")


def _define_agent(db: HubDatabase, name: str, include: list[str]) -> None:
    body = make_agent_definition(
        prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
        name=name,
        workflows=make_agent_workflows(
            rules=[],
            rule_selectors=AgentSelector(include=include, exclude=[]),
        ),
    )
    AgentDefinitionManager(db).create(name=name, definition_json=body.model_dump(), source="custom")


def _call_tool_event(mcp_tool: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": mcp_tool,
                "arguments": {"session_id": "22222222-2222-4222-8222-222222222222"},
            },
        },
    )


def _spawned_variables(agent_type: str) -> dict[str, Any]:
    return {
        "_agent_type": agent_type,
        "is_spawned_agent": True,
        # An agent block-list that would otherwise cover the terminal tools: the
        # operator-tool bypass must not skip YAML rule evaluation.
        "_agent_blocked_mcp_tools": ["gobby-sessions:*"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_tool", TERMINAL_TOOLS)
async def test_selected_rule_blocks_spawned_agent_terminal_tools(
    temp_db: HubDatabase, mcp_tool: str
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    _define_agent(temp_db, "seat-with-rule", ["tag:default", "name:no-send-keys"])

    response = await RuleEngine(temp_db).evaluate(
        _call_tool_event(mcp_tool),
        session_id=SESSION_ID,
        variables=_spawned_variables("seat-with-rule"),
    )

    assert response.decision == "block"
    assert "does not grant terminal control" in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_tool", TERMINAL_TOOLS)
async def test_unselected_rule_leaves_spawned_agent_terminal_tools(
    temp_db: HubDatabase, mcp_tool: str
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    _define_agent(temp_db, "seat-without-rule", ["tag:default"])

    response = await RuleEngine(temp_db).evaluate(
        _call_tool_event(mcp_tool),
        session_id=SESSION_ID,
        variables=_spawned_variables("seat-without-rule"),
    )

    assert response.decision == "allow"
    assert "operator-tool-enforcement" not in (response.reason or "")
    assert "does not grant terminal control" not in (response.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_tool", TERMINAL_TOOLS)
async def test_unresolved_default_root_session_keeps_terminal_tools(
    temp_db: HubDatabase, mcp_tool: str
) -> None:
    # No `default` definition row: the engine applies every enabled rule, so the
    # rule's own guard must keep it off the operator's root session.
    sync_bundled_rules(temp_db, get_bundled_rules_path())

    response = await RuleEngine(temp_db).evaluate(
        _call_tool_event(mcp_tool),
        session_id=SESSION_ID,
        variables={"_agent_type": "default"},
    )

    assert "does not grant terminal control" not in (response.reason or "")


@pytest.mark.asyncio
async def test_selected_rule_leaves_other_session_tools(temp_db: HubDatabase) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    _define_agent(temp_db, "seat-with-rule", ["tag:default", "name:no-send-keys"])

    response = await RuleEngine(temp_db).evaluate(
        _call_tool_event("list_sessions"),
        session_id=SESSION_ID,
        variables={"_agent_type": "seat-with-rule", "is_spawned_agent": True},
    )

    assert "does not grant terminal control" not in (response.reason or "")


@pytest.mark.parametrize(
    ("source", "event_data", "variables", "expected"),
    [
        # A spawned Gobby agent is not a subagent: the no-send-keys rule gates it.
        (SessionSource.CLAUDE, {}, {"is_spawned_agent": True}, False),
        (SessionSource.CODEX, {}, {"is_spawned_agent": True}, False),
        # A payload-named native subagent is one on any CLI.
        (SessionSource.CLAUDE, {"agent_id": "agent-uuid"}, {"is_spawned_agent": True}, True),
        (SessionSource.CLAUDE, {"agent_id": "agent-uuid"}, {}, True),
        # Claude omits the key on the parent thread even while is_subagent is set.
        (SessionSource.CLAUDE, {}, {"is_subagent": True}, False),
        # CLIs without per-event identity keep the session flag.
        (SessionSource.CODEX, {}, {"is_subagent": True}, True),
    ],
)
def test_operator_event_is_subagent_ignores_spawned_agents(
    source: SessionSource,
    event_data: dict[str, Any],
    variables: dict[str, Any],
    expected: bool,
) -> None:
    assert operator_event_is_subagent(event_data, source, variables) is expected
