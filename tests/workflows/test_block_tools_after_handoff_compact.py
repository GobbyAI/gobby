"""Integration tests for the pending handoff-compaction tool gate."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
RULE_NAME = "block-tools-after-handoff-compact"
BLOCK_REASON = "Stop calling tools and end your turn now so the queued compaction runs"


@pytest.fixture
def handler(temp_db: HubDatabase) -> WorkflowHookHandler:
    """Load only the bundled pending-compaction rule into the real rule engine."""
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    with temp_db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET source = 'installed', enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
            (RULE_NAME,),
        )
    return WorkflowHookHandler(rule_engine=RuleEngine(temp_db))


def _event(
    event_type: HookEventType,
    *,
    data: dict[str, Any] | None = None,
    session_type: str = "terminal",
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="provider-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata={
            "_platform_session_id": SESSION_ID,
            "session_type": session_type,
        },
    )


def _set_handoff_event(
    tool_output: dict[str, Any],
    *,
    session_type: str = "terminal",
    clear_session: bool = False,
) -> HookEvent:
    return _event(
        HookEventType.AFTER_TOOL,
        session_type=session_type,
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": "set_handoff",
                "arguments": {
                    "current_state": "Implementation is in progress.",
                    "next_steps": ["Resume after compaction."],
                    "clear_session": clear_session,
                },
            },
            "tool_output": tool_output,
            "mcp_server": "gobby-sessions",
            "mcp_tool": "set_handoff",
        },
    )


def _arbitrary_tool_event(*, session_type: str = "terminal") -> HookEvent:
    return _event(
        HookEventType.BEFORE_TOOL,
        session_type=session_type,
        data={"tool_name": "Bash", "tool_input": {"command": "pwd"}},
    )


@pytest.mark.asyncio
async def test_before_tool_blocked_after_successful_set_handoff(
    handler: WorkflowHookHandler,
    temp_db: HubDatabase,
) -> None:
    after_handoff = await handler._evaluate_rules(
        _set_handoff_event({"success": True, "result": {"compacted": True}})
    )
    stored = SessionVariableManager(temp_db).get_variables(SESSION_ID)

    response = await handler._evaluate_rules(_arbitrary_tool_event())

    assert after_handoff.decision == "allow"
    assert stored["context_compact_handoff_result"] == {
        "compacted": True,
        "reason": None,
    }
    assert response.decision == "block"
    assert BLOCK_REASON in (response.reason or "")


@pytest.mark.asyncio
async def test_before_tool_not_blocked_after_failed_set_handoff(
    handler: WorkflowHookHandler,
) -> None:
    await handler._evaluate_rules(
        _set_handoff_event(
            {
                "success": True,
                "result": {"compacted": False, "reason": "interrupt unconfirmed"},
            }
        )
    )

    response = await handler._evaluate_rules(_arbitrary_tool_event())

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_block_released_at_next_turn_start(handler: WorkflowHookHandler) -> None:
    await handler._evaluate_rules(
        _set_handoff_event({"success": True, "result": {"compacted": True}})
    )
    blocked = await handler._evaluate_rules(_arbitrary_tool_event())

    await handler._evaluate_rules(_event(HookEventType.BEFORE_AGENT))
    response = await handler._evaluate_rules(_arbitrary_tool_event())

    assert blocked.decision == "block"
    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_before_tool_not_blocked_for_web_chat(handler: WorkflowHookHandler) -> None:
    await handler._evaluate_rules(
        _set_handoff_event(
            {"success": True, "result": {"compacted": True}},
            session_type="web_chat",
        )
    )

    response = await handler._evaluate_rules(_arbitrary_tool_event(session_type="web_chat"))

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_before_tool_not_blocked_after_clear_session(
    handler: WorkflowHookHandler,
) -> None:
    await handler._evaluate_rules(
        _set_handoff_event(
            {"success": True, "successor_id": "successor-session"},
            clear_session=True,
        )
    )

    response = await handler._evaluate_rules(_arbitrary_tool_event())

    assert response.decision == "allow"
