"""Integration tests for the pending handoff-compaction tool gate."""

from __future__ import annotations

from dataclasses import dataclass
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
RETRY_RULE_NAME = "retry-terminal-handoff-after-delivery-failure"
NUDGE_RULE_NAMES = (
    "nudge-compact-on-context-pressure",
    "nudge-compact-on-context-pressure-mid-turn",
)
BLOCK_REASON = "Stop calling tools and end your turn now so the queued command runs"
EFFECT_COPY = "persists the handoff, then compacts the current session in place"
# The nested result as the proxy delivers it: the tool's ``success`` key is stripped.
STAGED_RESULT = {
    "handoff_staged": True,
    "delivery_pending": True,
    "attempt_id": "a" * 32,
    "session_id": SESSION_ID,
    "clear_session": False,
}


@dataclass
class _Session:
    context_used_tokens: int = 0
    context_window: int = 200_000
    session_type: str = "terminal"
    chat_mode: str = "normal"
    transcript_path: str | None = None


class _SessionManager:
    def __init__(self) -> None:
        self.session = _Session()

    def get(self, _session_id: str) -> _Session:
        return self.session


@pytest.fixture
def session_manager() -> Any:
    return _SessionManager()


@pytest.fixture
def handler(temp_db: HubDatabase, session_manager: Any) -> WorkflowHookHandler:
    """Load the bundled pending-compaction and context-nudge rules."""
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    with temp_db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET source = 'installed', enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name IN (%s, %s, %s, %s)",
            (RULE_NAME, RETRY_RULE_NAME, *NUDGE_RULE_NAMES),
        )
    return WorkflowHookHandler(
        rule_engine=RuleEngine(temp_db),
        session_manager=session_manager,
    )


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
            "tool_outcome": {"status": "succeeded"},
        },
    )


def _arbitrary_tool_event(*, session_type: str = "terminal") -> HookEvent:
    return _event(
        HookEventType.BEFORE_TOOL,
        session_type=session_type,
        data={"tool_name": "Bash", "tool_input": {"command": "pwd"}},
    )


def _arbitrary_after_tool_event() -> HookEvent:
    return _event(
        HookEventType.AFTER_TOOL,
        data={"tool_name": "Read", "tool_input": {"file_path": "/repo/a.py"}},
    )


@pytest.mark.asyncio
async def test_nudge_fires_once_per_threshold_crossing_with_real_engine(
    handler: WorkflowHookHandler,
    temp_db: HubDatabase,
    session_manager: Any,
) -> None:
    session_manager.session.context_used_tokens = 100_000

    soft = await handler._evaluate_rules(_event(HookEventType.BEFORE_AGENT))
    repeated_soft = await handler._evaluate_rules(_arbitrary_after_tool_event())
    session_manager.session.context_used_tokens = 150_000
    strong = await handler._evaluate_rules(_arbitrary_after_tool_event())
    repeated_strong = await handler._evaluate_rules(_arbitrary_after_tool_event())
    stored = SessionVariableManager(temp_db).get_variables(SESSION_ID)

    assert "Context is 100k tokens" in (soft.context or "")
    assert EFFECT_COPY in (soft.context or "")
    assert repeated_soft.context is None
    assert "Context is 150k tokens" in (strong.context or "")
    assert EFFECT_COPY in (strong.context or "")
    assert repeated_strong.context is None
    assert stored["context_compact_highest_announced_threshold"] == "strong"


@pytest.mark.asyncio
async def test_handoff_marker_suppresses_nudges_and_blocks_tools_until_turn_start(
    handler: WorkflowHookHandler,
    temp_db: HubDatabase,
    session_manager: Any,
) -> None:
    session_manager.session.context_used_tokens = 150_000

    after_handoff = await handler._evaluate_rules(
        _set_handoff_event({"success": True, "result": STAGED_RESULT})
    )
    after_tool = await handler._evaluate_rules(_arbitrary_after_tool_event())
    before_tool = await handler._evaluate_rules(_arbitrary_tool_event())
    turn_end = await handler._evaluate_rules(_event(HookEventType.STOP))
    stored_pending = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    successor_start = await handler._evaluate_rules(_event(HookEventType.BEFORE_AGENT))
    stored_successor = SessionVariableManager(temp_db).get_variables(SESSION_ID)

    assert after_handoff.context is None
    assert after_tool.context is None
    assert before_tool.context is None
    assert before_tool.decision == "block"
    assert BLOCK_REASON in (before_tool.reason or "")
    assert turn_end.context is None
    assert stored_pending["context_compact_handoff_result"]["delivery_pending"] is True
    assert "Context is 150k tokens" in (successor_start.context or "")
    assert stored_successor["context_compact_handoff_result"] is None


@pytest.mark.asyncio
async def test_failed_handoff_keeps_real_engine_nudges_enabled(
    handler: WorkflowHookHandler,
    session_manager: Any,
) -> None:
    session_manager.session.context_used_tokens = 150_000

    failed = await handler._evaluate_rules(
        _set_handoff_event({"success": True, "result": {"compacted": False, "reason": "no pane"}})
    )
    before_tool = await handler._evaluate_rules(_arbitrary_tool_event())

    assert "set_handoff could not compact (no pane)" in (failed.context or "")
    assert EFFECT_COPY in (failed.context or "")
    assert before_tool.decision == "allow"


@pytest.mark.asyncio
async def test_pending_clear_session_attempt_suppresses_real_engine_nudges(
    handler: WorkflowHookHandler,
    temp_db: HubDatabase,
    session_manager: Any,
) -> None:
    session_manager.session.context_used_tokens = 150_000
    pending = {**STAGED_RESULT, "clear_session": True}

    after_handoff = await handler._evaluate_rules(
        _set_handoff_event({"success": True, "result": pending}, clear_session=True)
    )
    after_tool = await handler._evaluate_rules(_arbitrary_after_tool_event())
    before_tool = await handler._evaluate_rules(_arbitrary_tool_event())
    stored = SessionVariableManager(temp_db).get_variables(SESSION_ID)

    assert after_handoff.context is None
    assert after_tool.context is None
    assert before_tool.decision == "block"
    assert stored["context_compact_handoff_result"]["delivery_pending"] is True


@pytest.mark.asyncio
async def test_before_tool_blocked_after_successful_set_handoff(
    handler: WorkflowHookHandler,
    temp_db: HubDatabase,
) -> None:
    after_handoff = await handler._evaluate_rules(
        _set_handoff_event({"success": True, "result": STAGED_RESULT})
    )
    stored = SessionVariableManager(temp_db).get_variables(SESSION_ID)

    response = await handler._evaluate_rules(_arbitrary_tool_event())

    assert after_handoff.decision == "allow"
    assert stored["context_compact_handoff_result"] == {
        "handoff_staged": True,
        "delivery_pending": True,
        "attempt_id": "a" * 32,
        "clear_session": False,
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
    await handler._evaluate_rules(_set_handoff_event({"success": True, "result": STAGED_RESULT}))
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
            {
                "success": True,
                "result": {**STAGED_RESULT, "clear_session": True},
            },
            clear_session=True,
        )
    )

    response = await handler._evaluate_rules(_arbitrary_tool_event())

    assert response.decision == "block"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_output",
    [
        {"success": False, "error": "provider failure"},
        {"success": True, "result": {"success": False, "error": "staging failed"}},
        {"success": True, "result": {"handoff_staged": True}},
        {
            "success": True,
            "result": {**STAGED_RESULT, "session_id": "wrong-session"},
        },
    ],
)
async def test_failed_or_malformed_handoff_never_arms_gate(
    handler: WorkflowHookHandler,
    tool_output: dict[str, Any],
) -> None:
    await handler._evaluate_rules(_set_handoff_event(tool_output))

    response = await handler._evaluate_rules(_arbitrary_tool_event())

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_background_delivery_failure_blocks_until_set_handoff_retry(
    handler: WorkflowHookHandler,
    temp_db: HubDatabase,
) -> None:
    SessionVariableManager(temp_db).merge_variables(
        SESSION_ID,
        {
            "context_compact_handoff_result": {
                "compacted": False,
                "delivery_failed": True,
                "retry_guidance": "Retry gobby-sessions:set_handoff now.",
            }
        },
    )

    blocked = await handler._evaluate_rules(_arbitrary_tool_event())
    retry = await handler._evaluate_rules(_set_handoff_event({"success": False}))

    assert blocked.decision == "block"
    assert "Retry gobby-sessions:set_handoff" in (blocked.reason or "")
    assert retry.decision == "allow"
