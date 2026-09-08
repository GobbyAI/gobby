"""Integration tests for the pending handoff-compaction tool gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
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
LIMIT_RULE_NAME = "require-handoff-at-context-limit"
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
    return _make_handler(temp_db, session_manager)


def _context_handoff_config(**overrides: Any) -> SimpleNamespace:
    values = {
        "warn_tokens": 200_000,
        "block_tokens": 256_000,
        "small_window_tokens": 256_000,
        "small_window_warn_ratio": 0.50,
        "small_window_block_ratio": 0.75,
        "extended_window_tokens": 500_000,
        "extended_warn_tokens": 250_000,
        "extended_block_tokens": 300_000,
        "warn_every_tool_calls": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _make_handler(
    temp_db: HubDatabase,
    session_manager: Any,
    *,
    context_handoff: SimpleNamespace | None = None,
) -> WorkflowHookHandler:
    """Load the bundled pending-compaction and context-nudge rules."""
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    with temp_db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET source = 'installed', enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name IN (%s, %s, %s, %s, %s)",
            (RULE_NAME, LIMIT_RULE_NAME, RETRY_RULE_NAME, *NUDGE_RULE_NAMES),
        )
    return WorkflowHookHandler(
        rule_engine=RuleEngine(temp_db),
        session_manager=session_manager,
        config=SimpleNamespace(
            workflow=SimpleNamespace(enabled=True, timeout=5.0),
            context_handoff=context_handoff or _context_handoff_config(),
        ),
    )


def _event(
    event_type: HookEventType,
    *,
    data: dict[str, Any] | None = None,
    session_type: str = "terminal",
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="provider-session",
        source=source,
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


def _arbitrary_tool_event(
    *,
    session_type: str = "terminal",
    source: SessionSource = SessionSource.CLAUDE,
    tool_name: str = "Bash",
    mcp_server: str | None = None,
    mcp_tool: str | None = None,
) -> HookEvent:
    data: dict[str, Any] = {"tool_name": tool_name, "tool_input": {"command": "pwd"}}
    if mcp_server is not None:
        data["mcp_server"] = mcp_server
    if mcp_tool is not None:
        data["mcp_tool"] = mcp_tool
    return _event(
        HookEventType.BEFORE_TOOL,
        session_type=session_type,
        source=source,
        data=data,
    )


def _arbitrary_after_tool_event(
    *,
    session_type: str = "terminal",
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    return _event(
        HookEventType.AFTER_TOOL,
        session_type=session_type,
        source=source,
        data={"tool_name": "Read", "tool_input": {"file_path": "/repo/a.py"}},
    )


@pytest.mark.asyncio
async def test_warn_nudge_repeats_every_configured_tool_count_and_turn_start(
    handler: WorkflowHookHandler,
    temp_db: HubDatabase,
    session_manager: Any,
) -> None:
    session_manager.session.context_window = 1_000_000
    session_manager.session.context_used_tokens = 240_000

    below = await handler._evaluate_rules(_event(HookEventType.BEFORE_AGENT))
    session_manager.session.context_used_tokens = 250_000
    first_turn = await handler._evaluate_rules(_event(HookEventType.BEFORE_AGENT))
    first_tool = await handler._evaluate_rules(_arbitrary_after_tool_event())
    middle_tools = [await handler._evaluate_rules(_arbitrary_after_tool_event()) for _ in range(3)]
    cadence_tool = await handler._evaluate_rules(_arbitrary_after_tool_event())
    next_turn = await handler._evaluate_rules(_event(HookEventType.BEFORE_AGENT))
    stored = SessionVariableManager(temp_db).get_variables(SESSION_ID)

    assert below.context is None
    assert "Context is 250k tokens" in (first_turn.context or "")
    assert EFFECT_COPY in (first_turn.context or "")
    assert first_tool.context is None
    assert all(response.context is None for response in middle_tools)
    assert "Context is 250k tokens" in (cadence_tool.context or "")
    assert "Context is 250k tokens" in (next_turn.context or "")
    assert stored["context_compact_mid_turn_pressure_band"] == "warn"
    assert stored["context_compact_tool_calls_since_nudge"] == 0


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
    assert successor_start.context is None
    assert stored_successor["context_compact_handoff_result"] is None
    assert stored_successor["context_compact_mid_turn_pressure_band"] == "none"


@pytest.mark.asyncio
async def test_failed_handoff_keeps_real_engine_nudges_enabled(
    handler: WorkflowHookHandler,
    session_manager: Any,
) -> None:
    session_manager.session.context_used_tokens = 125_000

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


@pytest.mark.asyncio
async def test_context_limit_blocks_bash_with_self_contained_handoff_sequence(
    handler: WorkflowHookHandler,
    session_manager: Any,
) -> None:
    session_manager.session.context_window = 1_000_000
    session_manager.session.context_used_tokens = 300_000

    pressure = await handler._evaluate_rules(_arbitrary_after_tool_event())
    blocked = await handler._evaluate_rules(_arbitrary_tool_event())

    assert "Context is 300k tokens" in (pressure.context or "")
    assert blocked.decision == "block"
    assert "300k" in (blocked.reason or "")
    assert "get_tool_schema" in (blocked.reason or "")
    assert "set_handoff" in (blocked.reason or "")


@pytest.mark.parametrize(
    "tool_event",
    [
        pytest.param(
            {"mcp_server": "gobby-sessions", "mcp_tool": "set_handoff"},
            id="set-handoff",
        ),
        pytest.param(
            {"mcp_server": "gobby-sessions", "mcp_tool": "feedback"},
            id="feedback",
        ),
        pytest.param(
            {"mcp_server": "gobby-sessions", "mcp_tool": "get_handoff"},
            id="get-handoff",
        ),
        pytest.param(
            {"mcp_server": "gobby-memory", "mcp_tool": "review_task_memories"},
            id="review-task-memories",
        ),
        pytest.param(
            {"mcp_server": "gobby-agents", "mcp_tool": "end_agent_run"},
            id="end-agent-run",
        ),
        pytest.param({"tool_name": "get_tool_schema"}, id="bare-get-tool-schema"),
        pytest.param({"tool_name": "list_tools"}, id="bare-list-tools"),
        pytest.param(
            {"tool_name": "mcp__gobby__get_tool_schema"},
            id="proxy-get-tool-schema",
        ),
        pytest.param({"tool_name": "mcp__gobby__list_tools"}, id="proxy-list-tools"),
    ],
)
@pytest.mark.asyncio
async def test_context_limit_allows_handoff_prerequisite_tools(
    handler: WorkflowHookHandler,
    session_manager: Any,
    tool_event: dict[str, str],
) -> None:
    session_manager.session.context_window = 1_000_000
    session_manager.session.context_used_tokens = 300_000
    await handler._evaluate_rules(_arbitrary_after_tool_event())

    allowed = await handler._evaluate_rules(
        _arbitrary_tool_event(
            tool_name=tool_event.get("tool_name", "Bash"),
            mcp_server=tool_event.get("mcp_server"),
            mcp_tool=tool_event.get("mcp_tool"),
        )
    )

    assert allowed.decision == "allow"


@pytest.mark.parametrize(
    ("plan_mode", "session_type", "source"),
    [
        pytest.param(True, "terminal", SessionSource.CLAUDE, id="plan-mode"),
        pytest.param(False, "web_chat", SessionSource.CLAUDE, id="web-chat"),
        pytest.param(False, "terminal", SessionSource.PIPELINE, id="pipeline"),
    ],
)
@pytest.mark.asyncio
async def test_context_limit_skips_exempt_session_modes_and_sources(
    handler: WorkflowHookHandler,
    temp_db: HubDatabase,
    session_manager: Any,
    plan_mode: bool,
    session_type: str,
    source: SessionSource,
) -> None:
    session_manager.session.context_window = 1_000_000
    session_manager.session.context_used_tokens = 300_000
    if plan_mode:
        SessionVariableManager(temp_db).merge_variables(SESSION_ID, {"plan_mode": True})

    pressure = await handler._evaluate_rules(
        _arbitrary_after_tool_event(session_type=session_type, source=source)
    )
    allowed = await handler._evaluate_rules(
        _arbitrary_tool_event(session_type=session_type, source=source)
    )

    assert allowed.decision == "allow"
    if plan_mode:
        assert pressure.context is None


@pytest.mark.asyncio
async def test_context_limit_lifts_after_compaction_lifecycle_and_lower_usage(
    handler: WorkflowHookHandler,
    temp_db: HubDatabase,
    session_manager: Any,
) -> None:
    session_manager.session.context_window = 1_000_000
    session_manager.session.context_used_tokens = 300_000
    await handler._evaluate_rules(_arbitrary_after_tool_event())
    assert (await handler._evaluate_rules(_arbitrary_tool_event())).decision == "block"

    await handler._evaluate_rules(_set_handoff_event({"success": True, "result": STAGED_RESULT}))
    await handler._evaluate_rules(_event(HookEventType.PRE_COMPACT))
    await handler._evaluate_rules(_event(HookEventType.SESSION_START, data={"source": "compact"}))
    await handler._evaluate_rules(_event(HookEventType.BEFORE_AGENT))
    session_manager.session.context_used_tokens = 120_000
    await handler._evaluate_rules(_arbitrary_after_tool_event())
    allowed = await handler._evaluate_rules(_arbitrary_tool_event())
    stored = SessionVariableManager(temp_db).get_variables(SESSION_ID)

    assert allowed.decision == "allow"
    assert stored["context_compact_mid_turn_pressure_band"] == "none"
    assert stored["context_compact_block_message"] == ""


@pytest.mark.asyncio
async def test_context_limit_uses_live_config_override(
    temp_db: HubDatabase,
    session_manager: Any,
) -> None:
    configured = _make_handler(
        temp_db,
        session_manager,
        context_handoff=_context_handoff_config(
            warn_tokens=10_000,
            block_tokens=20_000,
            small_window_tokens=300_000,
            small_window_warn_ratio=0.25,
            small_window_block_ratio=0.50,
            extended_window_tokens=350_000,
            extended_warn_tokens=15_000,
            extended_block_tokens=20_000,
            warn_every_tool_calls=2,
        ),
    )
    session_manager.session.context_window = 400_000
    session_manager.session.context_used_tokens = 20_000

    await configured._evaluate_rules(_arbitrary_after_tool_event())
    blocked = await configured._evaluate_rules(_arbitrary_tool_event())

    assert blocked.decision == "block"
    assert "20k" in (blocked.reason or "")


@pytest.mark.asyncio
async def test_non_retryable_handoff_failure_downgrades_block_to_warning(
    handler: WorkflowHookHandler,
    temp_db: HubDatabase,
    session_manager: Any,
) -> None:
    session_manager.session.context_window = 1_000_000
    session_manager.session.context_used_tokens = 300_000
    await handler._evaluate_rules(_arbitrary_after_tool_event())
    assert (await handler._evaluate_rules(_arbitrary_tool_event())).decision == "block"

    failed = await handler._evaluate_rules(
        _set_handoff_event(
            {
                "success": True,
                "result": {
                    "compacted": False,
                    "reason": "No live terminal target is available.",
                    "error_code": "terminal_target_unavailable",
                },
            }
        )
    )
    allowed = await handler._evaluate_rules(_arbitrary_tool_event())
    stored = SessionVariableManager(temp_db).get_variables(SESSION_ID)

    assert "set_handoff could not compact" in (failed.context or "")
    assert allowed.decision == "allow"
    assert stored["context_compact_mid_turn_pressure_band"] == "warn"
    assert stored["context_compact_handoff_unavailable"] is True
