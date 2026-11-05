"""A schema-lease refusal stops counting once the lease exists.

``get_tool_schema`` records its lease server-side, so a client whose schema
fetch emits no hook event never shows the engine a different tool between the
refused call and its retry. The retry of the same call after the fetch is the
lease rule's remediation: it resets the blocked-attempt count instead of
charging it, so the retry-limit escalation never locks the client out.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "44444444-4444-4444-8444-444444444444"
SERVER = "gobby-tasks"
TOOL = "get_task"


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    """Bundled retry-limit and progressive-discovery rules only."""
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    for row in RuleDefinitionManager(temp_db).list_all():
        armed = RuleDefinitionBody.model_validate(row.definition_json).retry_limit or (
            "progressive-discovery" in (row.tags or [])
        )
        temp_db.execute("UPDATE rule_definitions SET enabled = %s WHERE id = %s", (armed, row.id))
    return RuleEngine(temp_db)


def _event(event_type: HookEventType, data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


def _call() -> HookEvent:
    return _event(
        HookEventType.BEFORE_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": SERVER,
            "mcp_tool": TOOL,
            "tool_input": {"server_name": SERVER, "tool_name": TOOL, "arguments": {}},
        },
    )


@pytest.mark.asyncio
async def test_retry_after_the_schema_fetch_passes(engine: RuleEngine) -> None:
    variables: dict[str, Any] = {
        "enforce_tool_schema_check": True,
        "unlocked_tools": [],
        "max_consecutive_blocked_tool_attempts": 2,
    }

    refused = await engine.evaluate(_call(), SESSION_ID, variables)
    assert refused.decision == "block"
    assert "get_tool_schema" in (refused.reason or "")

    await engine.evaluate(_event(HookEventType.POST_TOOL_BATCH), SESSION_ID, variables)
    escalated = await engine.evaluate(_call(), SESSION_ID, variables)
    assert escalated.decision == "block"
    assert variables["consecutive_tool_blocks"] == 1

    # The proxy records the lease when get_tool_schema succeeds; no hook event fires.
    await engine.evaluate(_event(HookEventType.POST_TOOL_BATCH), SESSION_ID, variables)
    variables["unlocked_tools"] = [f"{SERVER}:{TOOL}"]
    retried = await engine.evaluate(_call(), SESSION_ID, variables)

    assert retried.decision == "allow"
    assert variables["consecutive_tool_blocks"] == 0
