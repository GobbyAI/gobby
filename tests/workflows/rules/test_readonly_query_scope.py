"""Tests for the query_readonly scope rule: only the Assistant and Orchestrator may call it."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE = "query-readonly-seat-scope"
SESSION_ID = "66666666-6666-4666-8666-666666666666"
READ_ALLOWED = ("assistant", "orchestrator", "fixture-database-reader")
READ_REFUSED = ("default", "developer", "code-reviewer", "researcher", "dev-manager")


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    for name in READ_ALLOWED:
        AgentDefinitionManager(temp_db).create(
            name,
            {
                "name": name,
                "prompts": {"agent": "Fixture database reader."},
                "workflows": {
                    "rule_selectors": {
                        "include": ["tag:default"],
                        "exclude": [f"name:{RULE}"],
                    }
                },
            },
        )
    yield temp_db


def _only_rule_under_test_enabled(db: HubDatabase) -> None:
    manager = RuleDefinitionManager(db)
    for row in manager.list_all():
        if row.name != RULE and row.enabled:
            manager.update(row.id, enabled=False)


def _query_call() -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": "gobby-sessions",
            "tool_name": "query_readonly",
            "arguments": {"sql": "SELECT 1"},
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


async def _decision(db: HubDatabase, agent_name: str) -> str:
    _only_rule_under_test_enabled(db)
    response = await RuleEngine(db).evaluate(
        _query_call(), session_id=SESSION_ID, variables={"_agent_type": agent_name}
    )
    return response.decision


@pytest.mark.parametrize("agent_name", READ_ALLOWED)
async def test_assistant_and_orchestrator_may_query(db: HubDatabase, agent_name: str) -> None:
    assert await _decision(db, agent_name) == "allow"


@pytest.mark.parametrize("agent_name", READ_REFUSED)
async def test_other_seats_are_refused_the_query_tool(db: HubDatabase, agent_name: str) -> None:
    assert await _decision(db, agent_name) == "block"
