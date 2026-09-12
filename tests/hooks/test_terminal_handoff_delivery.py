"""Post-result terminal handoff dispatch contract."""

from __future__ import annotations

import logging
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
from gobby.sessions.handoff import (
    HANDOFF_DISPATCH_GATE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    ClaimedHandoffDelivery,
    staged_handoff_tool_result,
)

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION_ID = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = "a" * 32
LOGGER_NAME = "gobby.hooks.terminal_handoff_delivery"
TERMINAL_SOURCES = [
    SessionSource.CLAUDE,
    SessionSource.CODEX,
    SessionSource.GROK,
    SessionSource.QWEN,
    SessionSource.DROID,
]

# The call_tool envelope a Claude tmux session actually received on 2026-09-03
# (game-goblins S#98, #21713). The proxy strips the tool's top-level ``success``.
REAL_SET_HANDOFF_OUTPUT: dict[str, Any] = {
    "success": True,
    "result": {
        "handoff_staged": True,
        "delivery_pending": True,
        "attempt_id": "b2db8b6dbe1543c4b891f6a36324eefe",
        "session_id": SESSION_ID,
        "clear_session": False,
        "command": "/compact",
        "cli": "claude",
        "via": "tmux",
    },
    "response_time_ms": 663.4,
}


def _proxy_envelope(tool_result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a sub-tool result the way mcp_proxy/server.py hands it to the CLI."""
    return {
        "success": True,
        "result": {key: value for key, value in tool_result.items() if key != "success"},
        "response_time_ms": 1.0,
    }


def _staged_output(**overrides: Any) -> dict[str, Any]:
    """Derive the fixture from the tool's result builder so drift fails these tests."""
    result = staged_handoff_tool_result(
        attempt_id=ATTEMPT_ID,
        session_id=SESSION_ID,
        clear_session=False,
        command="/compact",
        cli="claude",
        via="tmux",
    )
    result.update(overrides)
    return _proxy_envelope(result)


def _completion(
    source: SessionSource,
    output: object,
    *,
    outcome: str | None = None,
) -> HookEvent:
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
    if outcome is not None:
        data["tool_outcome"] = {"status": outcome}
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="provider-session",
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        # Real AFTER_TOOL metadata carries no session_type; the tool result is
        # the terminal-session authority (#21713).
        metadata={"_platform_session_id": SESSION_ID},
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize("source", TERMINAL_SOURCES)
def test_successful_normalized_completion_returns_staged_dispatch(
    source: SessionSource,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    dispatch = staged_handoff_from_event(_completion(source, _staged_output()))

    assert dispatch is not None
    assert dispatch.session_id == SESSION_ID
    assert dispatch.attempt_id == ATTEMPT_ID
    assert dispatch.clear_session is False
    assert _warnings(caplog) == []


def test_staged_handoff_accepts_real_set_handoff_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    dispatch = staged_handoff_from_event(_completion(SessionSource.CLAUDE, REAL_SET_HANDOFF_OUTPUT))

    assert dispatch is not None
    assert dispatch.session_id == SESSION_ID
    assert dispatch.attempt_id == "b2db8b6dbe1543c4b891f6a36324eefe"
    assert dispatch.clear_session is False
    assert _warnings(caplog) == []


def test_fixture_matches_the_observed_tool_result_shape() -> None:
    derived = _staged_output()

    assert set(derived["result"]) == set(REAL_SET_HANDOFF_OUTPUT["result"])
    assert "success" not in derived["result"]


def test_clear_session_result_returns_clear_dispatch() -> None:
    dispatch = staged_handoff_from_event(
        _completion(
            SessionSource.CODEX,
            _staged_output(clear_session=True, command="/clear"),
        )
    )

    assert dispatch is not None
    assert dispatch.clear_session is True


@pytest.mark.parametrize(
    "output",
    [
        {"success": False, "error": "outer failure"},
        {"success": True, "result": {"compacted": False, "reason": "no pane"}},
        {"success": True, "result": {"handoff_staged": True}},
        {
            "success": True,
            "result": {"compacted": True, "handoff_staged": True, "attempt_id": ATTEMPT_ID},
        },
        "malformed",
    ],
    ids=["outer-failure", "compact-failed", "no-delivery-pending", "web-chat-sync", "string"],
)
def test_failed_or_synchronous_completion_never_dispatches(
    output: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    assert staged_handoff_from_event(_completion(SessionSource.CLAUDE, output)) is None
    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    ("make_event", "session_text", "attempt_text", "fragment"),
    [
        (
            lambda: _completion(SessionSource.CLAUDE, _staged_output(session_id=OTHER_SESSION_ID)),
            OTHER_SESSION_ID,
            ATTEMPT_ID,
            f"result session {OTHER_SESSION_ID} is not the hook session {SESSION_ID}",
        ),
        (
            lambda: _completion(SessionSource.PIPELINE, _staged_output()),
            SESSION_ID,
            ATTEMPT_ID,
            "session source 'pipeline' is not a terminal CLI",
        ),
        (
            lambda: _completion(SessionSource.CLAUDE, _staged_output(attempt_id="")),
            SESSION_ID,
            "unknown",
            "result has no attempt_id",
        ),
        (
            lambda: _completion(SessionSource.CLAUDE, "malformed", outcome="succeeded"),
            SESSION_ID,
            "unknown",
            "tool_output is str, not the call_tool envelope",
        ),
    ],
    ids=["wrong-session", "non-terminal-source", "no-attempt", "string"],
)
def test_skipped_delivery_is_logged(
    make_event: Callable[[], HookEvent],
    session_text: str,
    attempt_text: str,
    fragment: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    assert staged_handoff_from_event(make_event()) is None

    (message,) = _warnings(caplog)
    assert message == (
        f"Terminal handoff delivery skipped for session {session_text} "
        f"attempt {attempt_text}: {fragment}"
    )


def test_unclaimed_completion_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    event = _completion(SessionSource.CLAUDE, _staged_output())
    variable_manager = MagicMock()
    variable_manager.get_variables.return_value = {
        PENDING_HANDOFF_VARIABLE: {
            "attempt_id": ATTEMPT_ID,
            "dispatch_started_at": "2026-09-03T21:47:00+00:00",
            "clear_session": False,
            "handoff_record_id": "handoff-1",
        },
        HANDOFF_DISPATCH_GATE_VARIABLE: {
            "handoff_staged": True,
            "delivery_pending": True,
            "attempt_id": ATTEMPT_ID,
            "clear_session": False,
        },
    }

    with (
        patch(
            "gobby.hooks.terminal_handoff_delivery.claim_staged_handoff_delivery",
            return_value=None,
        ),
        patch(
            "gobby.hooks.terminal_handoff_delivery.SessionVariableManager",
            return_value=variable_manager,
        ),
    ):
        scheduled = schedule_terminal_handoff_delivery(
            event,
            session_manager=MagicMock(),
            agent_run_manager=MagicMock(),
            event_loop=MagicMock(),
        )

    assert scheduled is False
    assert _warnings(caplog) == [
        f"Terminal handoff delivery skipped for session {SESSION_ID} attempt {ATTEMPT_ID}: "
        "dispatch already started at 2026-09-03T21:47:00+00:00"
    ]


def test_duplicate_completion_schedules_only_the_claimed_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    event = _completion(SessionSource.CODEX, _staged_output())
    claimed = ClaimedHandoffDelivery(SESSION_ID, ATTEMPT_ID, "handoff-1", False)
    session_manager = MagicMock()
    settle = AsyncMock(return_value=None)
    event_loop = MagicMock()
    event_loop.is_closed.return_value = False
    scheduled = MagicMock()
    variable_manager = MagicMock()
    variable_manager.get_variables.return_value = {}

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
        patch(
            "gobby.hooks.terminal_handoff_delivery.SessionVariableManager",
            return_value=variable_manager,
        ),
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
    assert _warnings(caplog) == [
        f"Terminal handoff delivery skipped for session {SESSION_ID} attempt {ATTEMPT_ID}: "
        f"no {PENDING_HANDOFF_VARIABLE} marker"
    ]


async def test_after_tool_handler_routes_normalized_completion_to_scheduler() -> None:
    session_manager = MagicMock()
    agent_run_manager = MagicMock()
    event_loop = MagicMock()
    handlers = EventHandlers(
        session_manager=session_manager,
        agent_run_manager=agent_run_manager,
        event_loop=event_loop,
    )
    event = _completion(SessionSource.GROK, _staged_output())

    with patch(
        "gobby.hooks.event_handlers._tool.schedule_terminal_handoff_delivery",
        return_value=True,
    ) as schedule:
        response = await handlers.handle_after_tool(event)

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
    claimed = ClaimedHandoffDelivery(SESSION_ID, ATTEMPT_ID, "handoff-1", False)
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


def test_unclaimed_idle_attempt_is_failed_with_retry_guidance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    event = _completion(SessionSource.CLAUDE, _staged_output())
    variable_manager = MagicMock()
    variable_manager.get_variables.return_value = {
        PENDING_HANDOFF_VARIABLE: {
            "attempt_id": ATTEMPT_ID,
            "clear_session": False,
            "handoff_record_id": "handoff-1",
        }
    }
    restore = MagicMock(return_value=True)

    with (
        patch(
            "gobby.hooks.terminal_handoff_delivery.claim_staged_handoff_delivery",
            return_value=None,
        ),
        patch(
            "gobby.hooks.terminal_handoff_delivery.SessionVariableManager",
            return_value=variable_manager,
        ),
        patch("gobby.hooks.terminal_handoff_delivery.restore_staged_handoff", restore),
    ):
        scheduled = schedule_terminal_handoff_delivery(
            event,
            session_manager=MagicMock(),
            agent_run_manager=MagicMock(),
            event_loop=MagicMock(),
        )

    assert scheduled is False
    restore.assert_called_once()
    assert restore.call_args.args[1:] == (SESSION_ID, ATTEMPT_ID)
    failure = restore.call_args.kwargs["failure_result"]
    assert failure["delivery_failed"] is True
    assert failure["reason"] == f"{HANDOFF_DISPATCH_GATE_VARIABLE} gate is not armed"
    assert _warnings(caplog) == [
        f"Terminal handoff delivery failed for session {SESSION_ID} attempt {ATTEMPT_ID}: "
        f"{HANDOFF_DISPATCH_GATE_VARIABLE} gate is not armed"
    ]


def test_non_terminal_source_completion_is_failed_with_retry_guidance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    event = _completion(SessionSource.PIPELINE, _staged_output())
    restore = MagicMock(return_value=True)

    with patch("gobby.hooks.terminal_handoff_delivery.restore_staged_handoff", restore):
        scheduled = schedule_terminal_handoff_delivery(
            event,
            session_manager=MagicMock(),
            agent_run_manager=MagicMock(),
            event_loop=MagicMock(),
        )

    assert scheduled is False
    restore.assert_called_once()
    assert restore.call_args.args[1:] == (SESSION_ID, ATTEMPT_ID)
    failure = restore.call_args.kwargs["failure_result"]
    assert failure["reason"] == "session source 'pipeline' is not a terminal CLI"
    assert "Retry gobby-sessions:set_handoff" in failure["retry_guidance"]
    assert _warnings(caplog) == [
        f"Terminal handoff delivery failed for session {SESSION_ID} attempt {ATTEMPT_ID}: "
        "session source 'pipeline' is not a terminal CLI"
    ]
