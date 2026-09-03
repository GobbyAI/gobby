"""Post-result terminal handoff dispatch contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks import terminal_handoff_delivery
from gobby.hooks._normalization_tools import normalize_tool_fields
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.terminal_handoff_delivery import (
    schedule_terminal_handoff_delivery,
    staged_handoff_from_event,
)
from gobby.sessions.handoff import ClaimedHandoffDelivery

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _completion(source: SessionSource, output: object) -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": "gobby-sessions",
            "tool_name": "set_handoff",
            "arguments": {"clear_session": False},
        },
        "tool_output": output,
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="provider-session",
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": SESSION_ID, "session_type": "terminal"},
    )


@pytest.mark.parametrize(
    "source",
    [
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.GROK,
        SessionSource.QWEN,
        SessionSource.DROID,
    ],
)
def test_successful_normalized_completion_returns_staged_dispatch(source: SessionSource) -> None:
    staged = {
        "success": True,
        "handoff_staged": True,
        "delivery_pending": True,
        "attempt_id": "a" * 32,
        "session_id": SESSION_ID,
        "clear_session": False,
    }

    dispatch = staged_handoff_from_event(_completion(source, {"success": True, "result": staged}))

    assert dispatch is not None
    assert dispatch.session_id == SESSION_ID
    assert dispatch.attempt_id == "a" * 32
    assert dispatch.clear_session is False


@pytest.mark.parametrize(
    "output",
    [
        {"success": False, "error": "outer failure"},
        {"success": True, "result": {"success": False, "error": "inner failure"}},
        {"success": True, "result": {"success": True, "handoff_staged": True}},
        "malformed",
    ],
)
def test_failed_or_malformed_completion_never_dispatches(output: object) -> None:
    assert staged_handoff_from_event(_completion(SessionSource.CLAUDE, output)) is None


def test_duplicate_completion_schedules_only_the_claimed_attempt() -> None:
    event = _completion(
        SessionSource.CODEX,
        {
            "success": True,
            "result": {
                "success": True,
                "handoff_staged": True,
                "delivery_pending": True,
                "attempt_id": "a" * 32,
                "session_id": SESSION_ID,
                "clear_session": False,
            },
        },
    )
    claimed = ClaimedHandoffDelivery(SESSION_ID, "a" * 32, "handoff-1", False)
    session_manager = MagicMock()
    settle = AsyncMock(return_value=None)
    event_loop = MagicMock()
    event_loop.is_closed.return_value = False
    scheduled = MagicMock()

    with (
        patch(
            "gobby.hooks.terminal_handoff_delivery.claim_staged_handoff_delivery",
            side_effect=[claimed, None],
        ),
        patch("gobby.hooks.terminal_handoff_delivery._settle_delivery", settle),
        patch(
            "gobby.hooks.terminal_handoff_delivery.asyncio.run_coroutine_threadsafe",
            return_value=scheduled,
        ) as run_coroutine_threadsafe,
    ):
        first = schedule_terminal_handoff_delivery(
            event,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            event_loop=event_loop,
        )
        duplicate = schedule_terminal_handoff_delivery(
            event,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            event_loop=event_loop,
        )

    assert first is True
    assert duplicate is False
    settle.assert_called_once()
    run_coroutine_threadsafe.assert_called_once()
    coroutine = run_coroutine_threadsafe.call_args.args[0]
    coroutine.close()
    completion_callback = scheduled.add_done_callback.call_args.args[0]
    completion_callback(scheduled)


def test_after_tool_handler_routes_normalized_completion_to_scheduler() -> None:
    session_manager = MagicMock()
    agent_run_manager = MagicMock()
    event_loop = MagicMock()
    handlers = EventHandlers(
        session_manager=session_manager,
        agent_run_manager=agent_run_manager,
        event_loop=event_loop,
    )
    event = _completion(
        SessionSource.GROK,
        {
            "success": True,
            "result": {
                "success": True,
                "handoff_staged": True,
                "delivery_pending": True,
                "attempt_id": "a" * 32,
                "session_id": SESSION_ID,
                "clear_session": False,
            },
        },
    )

    with patch(
        "gobby.hooks.event_handlers._tool.schedule_terminal_handoff_delivery",
        return_value=True,
    ) as schedule:
        response = handlers.handle_after_tool(event)

    assert response.decision == "allow"
    assert schedule.call_count == 1
    call = schedule.call_args
    assert call.args == (event,)
    assert call.kwargs["session_manager"] is session_manager
    assert call.kwargs["agent_run_manager"] is agent_run_manager
    assert call.kwargs["event_loop"] is event_loop
    assert call.kwargs["terminal_manager"] is None
    assert call.kwargs["terminal_runtime_registry"] is None


@pytest.mark.asyncio
async def test_background_delivery_failure_compensates_with_retry_guidance() -> None:
    claimed = ClaimedHandoffDelivery(SESSION_ID, "a" * 32, "handoff-1", False)
    session_manager = MagicMock()
    restore = MagicMock(return_value=True)

    async def run_delivery(
        _run_id: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return await operation()

    with (
        patch(
            "gobby.hooks.terminal_handoff_delivery.deliver_staged_compact_handoff",
            new=AsyncMock(return_value={"compacted": False, "reason": "pane disappeared"}),
        ),
        patch(
            "gobby.hooks.terminal_handoff_delivery.shielded_terminal_delivery",
            side_effect=run_delivery,
        ),
        patch(
            "gobby.hooks.terminal_handoff_delivery.restore_staged_handoff",
            restore,
        ),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )

    restore.assert_called_once()
    failure = restore.call_args.kwargs["failure_result"]
    assert failure["delivery_failed"] is True
    assert failure["delivery_pending"] is False
    assert "Retry gobby-sessions:set_handoff" in failure["retry_guidance"]
