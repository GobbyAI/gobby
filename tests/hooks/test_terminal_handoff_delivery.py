"""Post-result terminal handoff dispatch contract."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.grok import GrokAdapter
from gobby.agents.idle_detector import IdleDetector
from gobby.hooks import terminal_handoff_delivery
from gobby.hooks._normalization_tools import normalize_tool_fields
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.event_handlers._session_start.materialize import (
    _consume_pending_handoff_compact_continuation,
)
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.terminal_handoff_delivery import (
    schedule_terminal_handoff_delivery,
    staged_handoff_from_event,
)
from gobby.sessions.compact_continuation import (
    _HANDOFF_COMPACT_CONTINUATION_TASKS,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
    mark_handoff_compact_continuation_pending,
)
from gobby.sessions.handoff import (
    HANDOFF_DELIVERY_FAILURES_VARIABLE,
    HANDOFF_DISPATCH_GATE_VARIABLE,
    HANDOFF_UNAVAILABLE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    ClaimedHandoffDelivery,
    build_handoff_continue_prompt,
    staged_handoff_tool_result,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.terminals.runtime import SnapshotMode
from gobby.workflows.state_manager import SessionVariableManager
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
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


def _infos(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno == logging.INFO
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


def test_grok_mcp_tool_result_returns_staged_dispatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Grok's post_tool_use toolResult is the tool's raw output, so an MCP result
    # arrives as {"type": "MCP", "output": {"OkayOutput": "<proxy JSON text>"}}
    # (gobby#13288's set_handoff was staged but never delivered).
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    envelope = _staged_output(cli="grok")
    event = GrokAdapter().translate_to_hook_event(
        {
            "hook_type": "post_tool_use",
            "input_data": {
                "hookEventName": "post_tool_use",
                "sessionId": "grok-session",
                "toolName": "gobby__call_tool",
                "toolInput": {
                    "tool_name": "gobby__call_tool",
                    "tool_input": {
                        "server_name": "gobby-sessions",
                        "tool_name": "set_handoff",
                        "arguments": {"clear_session": False},
                    },
                },
                "toolResult": {
                    "type": "MCP",
                    "tool_name": "call_tool",
                    "server_name": "gobby",
                    "output": {"OkayOutput": json.dumps(envelope, indent=2)},
                },
            },
        }
    )
    event.metadata["_platform_session_id"] = SESSION_ID

    dispatch = staged_handoff_from_event(event)

    assert event.data["tool_output"] == envelope
    assert dispatch is not None
    assert (dispatch.session_id, dispatch.attempt_id, dispatch.clear_session) == (
        SESSION_ID,
        ATTEMPT_ID,
        False,
    )
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
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
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
    assert _infos(caplog) == [
        f"Terminal handoff delivery scheduled for session {SESSION_ID} attempt {ATTEMPT_ID}"
    ]
    assert _warnings(caplog) == [
        f"Terminal handoff delivery skipped for session {SESSION_ID} attempt {ATTEMPT_ID}: "
        f"no {PENDING_HANDOFF_VARIABLE} marker"
    ]


def test_after_tool_handler_routes_normalized_completion_to_scheduler() -> None:
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


GROK_REJECTION = "'/compact' is disabled while a task is in progress"


class _RejectingGrokPane:
    """Grok pane fake: Ctrl+C cancels the turn in events.jsonl; /compact stays rejected."""

    backend = "native"
    target = "term-grok"

    def __init__(self, events_path: Path) -> None:
        self.keys: list[str] = []
        self.typed: list[str] = []
        self.screen = "working...\n> "
        self._events_path = events_path

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        self.keys.append(key)
        if key == "ctrl_c":
            with self._events_path.open("ab") as stream:
                stream.write(json.dumps({"type": "turn_ended", "outcome": "cancelled"}).encode())
                stream.write(b"\n")
        elif key == "enter":
            self.screen += f"\n{GROK_REJECTION}\n> "
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        self.typed.append(text)
        return True, None

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str | None:
        return self.screen


async def _run_operation(
    _run_id: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
    **_kwargs: Any,
) -> dict[str, Any]:
    return await operation()


@pytest.mark.asyncio
async def test_rejected_grok_compaction_settles_as_delivery_failed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    transcript = tmp_path / "updates.jsonl"
    transcript.write_bytes(b"")
    events = tmp_path / "events.jsonl"
    events.write_bytes(b"")
    pane = _RejectingGrokPane(events)
    session = SimpleNamespace(id=SESSION_ID, source="grok", transcript_path=str(transcript))
    session_manager = MagicMock()
    session_manager.get.return_value = session
    claimed = ClaimedHandoffDelivery(SESSION_ID, ATTEMPT_ID, "handoff-1", False)
    restore = MagicMock(return_value=True)
    clear_pending = MagicMock(return_value=True)

    with (
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery._resolve_pane_io",
            return_value=(pane, None),
        ),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery."
            "mark_handoff_compact_continuation_pending",
            return_value=True,
        ),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery."
            "clear_handoff_compact_continuation_pending",
            clear_pending,
        ),
        patch(
            "gobby.hooks.terminal_handoff_delivery.shielded_terminal_delivery",
            side_effect=_run_operation,
        ),
        patch("gobby.hooks.terminal_handoff_delivery.restore_staged_handoff", restore),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )

    # Grok is interrupted with Ctrl+C (never Esc) and the rejected /compact is retried once.
    assert pane.keys[0] == "ctrl_c"
    assert "escape" not in pane.keys
    assert pane.typed == ["/compact", "/compact"]
    clear_pending.assert_called_once()
    restore.assert_called_once()
    assert restore.call_args.args[1:] == (SESSION_ID, ATTEMPT_ID)
    failure = restore.call_args.kwargs["failure_result"]
    assert failure["delivery_failed"] is True
    assert failure["delivery_pending"] is False
    assert failure["reason"] == GROK_REJECTION
    assert _warnings(caplog) == [
        f"Terminal handoff delivery failed for session {SESSION_ID} attempt {ATTEMPT_ID}: "
        f"{GROK_REJECTION}"
    ]


@pytest.mark.asyncio
async def test_background_delivery_success_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    claimed = ClaimedHandoffDelivery(SESSION_ID, ATTEMPT_ID, "handoff-1", False)
    delivered = {"compacted": True, "command": "/compact", "cli": "grok", "via": "native"}
    deliver = AsyncMock(return_value=delivered)
    restore = MagicMock(return_value=True)

    with (
        patch("gobby.hooks.terminal_handoff_delivery.deliver_staged_compact_handoff", deliver),
        patch(
            "gobby.hooks.terminal_handoff_delivery.shielded_terminal_delivery",
            side_effect=_run_operation,
        ),
        patch("gobby.hooks.terminal_handoff_delivery.restore_staged_handoff", restore),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=MagicMock(),
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )

    deliver.assert_awaited_once()
    assert deliver.await_args is not None
    assert deliver.await_args.args == (SESSION_ID, ATTEMPT_ID, "handoff-1")
    restore.assert_not_called()
    assert _warnings(caplog) == []
    assert _infos(caplog) == [
        f"Terminal handoff delivered for session {SESSION_ID} attempt {ATTEMPT_ID} "
        "(clear_session=False cli=grok via=native)"
    ]


# Delivery failures are bounded per session: the first keeps today's retry
# guidance, the second abandons terminal delivery, lifts every handoff gate, and
# tells the agent not to stage again (occurrence 2 of #22364 re-staged nine times).
def _variable_manager(failures: int | None) -> MagicMock:
    manager = MagicMock()
    manager.get_variables.return_value = (
        {} if failures is None else {HANDOFF_DELIVERY_FAILURES_VARIABLE: failures}
    )
    manager.merge_variables.return_value = True
    return manager


async def _settle_failed_delivery(variable_manager: MagicMock) -> MagicMock:
    claimed = ClaimedHandoffDelivery(SESSION_ID, ATTEMPT_ID, "handoff-1", False)
    restore = MagicMock(return_value=True)
    with (
        patch(
            "gobby.hooks.terminal_handoff_delivery.deliver_staged_compact_handoff",
            new=AsyncMock(return_value={"compacted": False, "reason": "pane disappeared"}),
        ),
        patch(
            "gobby.hooks.terminal_handoff_delivery.shielded_terminal_delivery",
            side_effect=_run_operation,
        ),
        patch("gobby.hooks.terminal_handoff_delivery.restore_staged_handoff", restore),
        patch(
            "gobby.hooks.terminal_handoff_delivery.SessionVariableManager",
            return_value=variable_manager,
        ),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=MagicMock(),
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )
    return restore


@pytest.mark.asyncio
async def test_first_delivery_failure_counts_and_keeps_retry_guidance() -> None:
    variable_manager = _variable_manager(None)

    restore = await _settle_failed_delivery(variable_manager)

    failure = restore.call_args.kwargs["failure_result"]
    assert failure["delivery_failed"] is True
    assert failure.get("delivery_abandoned") is not True
    assert "Retry gobby-sessions:set_handoff" in failure["retry_guidance"]
    variable_manager.merge_variables.assert_called_once_with(
        SESSION_ID, {HANDOFF_DELIVERY_FAILURES_VARIABLE: 1}
    )


@pytest.mark.asyncio
async def test_second_consecutive_delivery_failure_abandons_terminal_handoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    variable_manager = _variable_manager(1)

    restore = await _settle_failed_delivery(variable_manager)

    failure = restore.call_args.kwargs["failure_result"]
    assert failure["compacted"] is False
    assert failure["delivery_failed"] is False
    assert failure["delivery_pending"] is False
    assert failure["delivery_abandoned"] is True
    assert failure["error_code"] == "handoff_delivery_abandoned"
    assert failure["reason"] == "pane disappeared"
    assert "Do not call set_handoff again" in failure["retry_guidance"]
    variable_manager.merge_variables.assert_called_once_with(
        SESSION_ID,
        {HANDOFF_DELIVERY_FAILURES_VARIABLE: 2, HANDOFF_UNAVAILABLE_VARIABLE: True},
    )
    assert _warnings(caplog) == [
        f"Terminal handoff delivery abandoned for session {SESSION_ID} after 2 consecutive "
        f"failures; attempt {ATTEMPT_ID}: pane disappeared"
    ]


@pytest.mark.asyncio
async def test_composer_occupied_delivery_failure_tells_the_agent_to_wait_for_the_operator() -> (
    None
):
    claimed = ClaimedHandoffDelivery(SESSION_ID, ATTEMPT_ID, "handoff-1", False)
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
            new=AsyncMock(
                return_value={
                    "compacted": False,
                    "reason": "composer holds an operator draft",
                    "error_code": "composer_occupied",
                }
            ),
        ),
        patch(
            "gobby.hooks.terminal_handoff_delivery.shielded_terminal_delivery",
            side_effect=run_delivery,
        ),
        patch("gobby.hooks.terminal_handoff_delivery.restore_staged_handoff", restore),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=MagicMock(),
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )

    failure = restore.call_args.kwargs["failure_result"]
    assert failure["error_code"] == "composer_occupied"
    assert failure["delivery_failed"] is True
    assert "unsent draft" in failure["retry_guidance"]
    assert "retry gobby-sessions:set_handoff" in failure["retry_guidance"]


# --- set_handoff -> compaction -> continuation through the terminal runtime (#22441) ---

_COMPACT_DELIVERY = "gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery"
_COMPACT_PROJECT_ID = "33333333-3333-4333-8333-333333333333"
_NATIVE_WORKER_CONTEXT = {"parent_pid": 12364, "gobby_session_id": SESSION_ID}


def _compact_session_manager(hub_db: HubDatabase, terminal_context: dict[str, Any]) -> Any:
    hub_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (_COMPACT_PROJECT_ID, "compact-continuation-delivery"),
    )
    hub_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
        "session_type, terminal_context) VALUES (%s, %s, %s, 'claude', %s, 'terminal', %s)",
        (
            SESSION_ID,
            SESSION_ID,
            "21000000-0000-4000-8000-000000000001",
            _COMPACT_PROJECT_ID,
            json.dumps(terminal_context),
        ),
    )
    return SessionManager(hub_db)


def _session_start_handler(session_manager: Any, store: Any, registry: Any) -> Any:
    return SimpleNamespace(
        _session_manager=session_manager,
        _session_coordinator=None,
        terminal_manager=store,
        _terminal_runtime_registry=registry,
    )


async def _await_continuations() -> None:
    await asyncio.gather(*list(_HANDOFF_COMPACT_CONTINUATION_TASKS))


async def test_native_worker_receives_the_continuation_after_set_handoff_compaction(
    hub_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_manager = _compact_session_manager(hub_db, _NATIVE_WORKER_CONTEXT)
    runtime = FakeRuntime(backend="native")
    store = MemoryTerminalStore(
        replace(make_memory_terminal(backend="native"), session_id=SESSION_ID)
    )
    registry = runtime_registry(runtime)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.sessions._terminal._COMPACTION_REJECTION_SETTLE_SECONDS", 0.0
    )
    monkeypatch.setattr(
        "gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS",
        0.0,
    )

    # set_handoff(clear_session=false) completed: the post-result delivery sends /compact.
    with (
        patch(f"{_COMPACT_DELIVERY}._interrupt_observer", return_value=(lambda: True, None)),
        patch(f"{_COMPACT_DELIVERY}._turn_settled_observer", return_value=lambda: True),
        patch(f"{_COMPACT_DELIVERY}.composer_reader", return_value=None),
        patch(f"{_COMPACT_DELIVERY}.clear_queued_context"),
        patch(f"{_COMPACT_DELIVERY}.record_handoff_delivery", return_value=True),
        patch(
            "gobby.hooks.terminal_handoff_delivery.shielded_terminal_delivery",
            side_effect=_run_operation,
        ),
    ):
        await terminal_handoff_delivery._settle_delivery(
            ClaimedHandoffDelivery(SESSION_ID, ATTEMPT_ID, "handoff-1", False),
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            terminal_manager=store,
            terminal_runtime_registry=registry,
        )
    compact_writes = len(runtime.write_log)
    assert ("text", "/compact") in runtime.write_log
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE in SessionVariableManager(hub_db).get_variables(
        SESSION_ID
    )

    # Claude restarts in place: SessionStart source=compact on the pre-created row.
    runtime.snapshot_text = ""
    handler = _session_start_handler(session_manager, store, registry)
    with patch(
        "gobby.sessions.compact_continuation._composer_reader",
        return_value=IdleDetector(BundledDetectionRegistry(), "claude").composer_read,
    ):
        scheduled = _consume_pending_handoff_compact_continuation(
            handler,
            session_source="compact",
            pending_session_id=SESSION_ID,
            target_session=session_manager.get(SESSION_ID),
        )
        await _await_continuations()

    assert scheduled is True
    # FakeRuntime records submit=True as a trailing newline; RuntimePaneIO strips the
    # newline it was given, so this entry is the prompt written and submitted natively.
    assert ("text", build_handoff_continue_prompt()) in runtime.write_log[compact_writes:]
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in SessionVariableManager(hub_db).get_variables(
        SESSION_ID
    )


async def test_tmux_pane_session_still_receives_the_continuation_by_tmux(
    hub_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_manager = _compact_session_manager(hub_db, {"tmux_pane": "%12"})
    tmux = MagicMock()
    tmux.dispatch_keys = AsyncMock(return_value=True)
    tmux.snapshot_lines = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "gobby.sessions.compact_continuation.SUBMIT_VERIFY_SECONDS",
        0.0,
    )
    assert mark_handoff_compact_continuation_pending(hub_db, SESSION_ID, attempt_id=ATTEMPT_ID)
    registry = runtime_registry(FakeRuntime(backend="native"))
    handler = _session_start_handler(session_manager, MemoryTerminalStore(), registry)

    with patch(
        "gobby.sessions.compact_continuation.manager_for_terminal_context", return_value=tmux
    ):
        scheduled = _consume_pending_handoff_compact_continuation(
            handler,
            session_source="compact",
            pending_session_id=SESSION_ID,
            target_session=session_manager.get(SESSION_ID),
        )
        await _await_continuations()

    assert scheduled is True
    tmux.dispatch_keys.assert_any_await("%12", build_handoff_continue_prompt(), literal=True)
