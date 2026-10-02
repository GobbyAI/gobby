"""Post-result terminal handoff dispatch contract."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.grok import GrokAdapter
from gobby.agents.idle_detector import ComposerRead, IdleDetector
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
from gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery import (
    _fresh_compact_error,
    _wait_for_compact_boundary,
)
from gobby.sessions import compact_continuation
from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE, stage_clear_attempt
from gobby.sessions.compact_continuation import (
    _HANDOFF_COMPACT_CONTINUATION_TASKS,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
    CompactBoundaryWaiter,
    mark_handoff_compact_continuation_pending,
)
from gobby.sessions.compact_markers import COMPACT_NOTIFICATION_STARTED_AT_VARIABLE
from gobby.sessions.handoff import (
    DISPATCH_OWNER,
    FAILED_HANDOFF_VARIABLE,
    FOUND_WORK_VARIABLE,
    HANDOFF_DELIVERY_FAILURES_VARIABLE,
    HANDOFF_DISPATCH_GATE_VARIABLE,
    HANDOFF_TURN_END_PENDING_VARIABLE,
    HANDOFF_UNAVAILABLE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    ClaimedHandoffDelivery,
    build_handoff_continue_prompt,
    claim_staged_handoff_delivery,
    stage_handoff_attempt,
    staged_handoff_tool_result,
)
from gobby.sessions.handoff_records import (
    FoundWorkEntry,
    build_handoff_payload,
    record_handoff_delivery,
)
from gobby.sessions.transcript_cursor import TranscriptTailCursor
from gobby.storage.attention import AttentionStateManager, session_attention_entry_id
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
            "dispatch_owner": DISPATCH_OWNER,
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
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        self.typed.append(text)
        # The write carries its own newline, so the command submits -- and is
        # rejected -- on the write rather than on a later Enter key.
        self.screen += f"\n{GROK_REJECTION}\n> "
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

    # Grok is interrupted with Ctrl+C (never Esc); a rejected /compact is not resubmitted.
    assert pane.keys[0] == "ctrl_c"
    assert "escape" not in pane.keys
    assert pane.typed == ["/compact\n"]
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


async def _settle_failed_delivery(
    variable_manager: MagicMock,
    result: dict[str, Any] | None = None,
) -> MagicMock:
    claimed = ClaimedHandoffDelivery(SESSION_ID, ATTEMPT_ID, "handoff-1", False)
    restore = MagicMock(return_value=True)
    delivered = result or {"compacted": False, "reason": "pane disappeared"}
    with (
        patch(
            "gobby.hooks.terminal_handoff_delivery.deliver_staged_compact_handoff",
            new=AsyncMock(return_value=delivered),
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
async def test_missing_seat_settles_without_counting_toward_abandonment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    variable_manager = _variable_manager(1)

    restore = await _settle_failed_delivery(
        variable_manager,
        {
            "compacted": False,
            "reason": "the recorded CLI process no longer owns its terminal",
            "error_code": "no_terminal_target",
        },
    )

    failure = restore.call_args.kwargs["failure_result"]
    assert failure["delivery_abandoned"] is False
    assert failure["delivery_pending"] is False
    assert failure["error_code"] == "no_terminal_target"
    assert "Do not call set_handoff again" in failure["retry_guidance"]
    assert f"failed_attempt_id={ATTEMPT_ID!r}" in failure["recovery_guidance"]
    variable_manager.merge_variables.assert_not_called()
    assert _warnings(caplog) == [
        f"Terminal handoff delivery failed for session {SESSION_ID} attempt {ATTEMPT_ID}: "
        "the recorded CLI process no longer owns its terminal"
    ]


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


def _compact_session_manager(
    hub_db: HubDatabase, terminal_context: dict[str, Any], *, source: str = "claude"
) -> Any:
    hub_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (_COMPACT_PROJECT_ID, "compact-continuation-delivery"),
    )
    hub_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
        "session_type, terminal_context) VALUES (%s, %s, %s, %s, %s, 'terminal', %s)",
        (
            SESSION_ID,
            SESSION_ID,
            "21000000-0000-4000-8000-000000000001",
            source,
            _COMPACT_PROJECT_ID,
            json.dumps(terminal_context),
        ),
    )
    return SessionManager(hub_db)


def test_stop_claims_staged_unclaimed_grok_handoff_before_stale_cutoff(
    hub_db: HubDatabase,
) -> None:
    session_manager = _compact_session_manager(hub_db, {"tmux_pane": "%12"}, source="grok")
    variables = SessionVariableManager(hub_db)
    variables.merge_variables(
        SESSION_ID,
        {
            PENDING_HANDOFF_VARIABLE: {
                "attempt_id": ATTEMPT_ID,
                "clear_session": False,
                "handoff_record_id": "handoff-1",
                "created_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    assert HANDOFF_DISPATCH_GATE_VARIABLE not in variables.get_variables(SESSION_ID)
    assert (
        hub_db.fetchone(
            "SELECT 1 FROM session_handoff_deliveries WHERE attempt_id = %s", (ATTEMPT_ID,)
        )
        is None
    )
    event_loop = MagicMock()
    event_loop.is_closed.return_value = False
    handlers = EventHandlers(
        session_manager=session_manager,
        agent_run_manager=MagicMock(),
        event_loop=event_loop,
    )
    event = HookEvent(
        event_type=HookEventType.STOP,
        session_id="provider-session",
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": SESSION_ID},
    )

    with (
        patch("gobby.hooks.terminal_handoff_delivery._settle_delivery", new_callable=AsyncMock),
        patch("gobby.hooks.terminal_handoff_delivery.asyncio.run_coroutine_threadsafe") as submit,
    ):
        response = handlers.handle_stop(event)

    marker = variables.get_variables(SESSION_ID)[PENDING_HANDOFF_VARIABLE]
    assert response.decision == "allow"
    assert marker["dispatch_started_at"] is not None
    assert variables.get_variables(SESSION_ID)[HANDOFF_DISPATCH_GATE_VARIABLE] == {
        "handoff_staged": True,
        "delivery_pending": True,
        "attempt_id": ATTEMPT_ID,
        "clear_session": False,
    }
    submit.assert_called_once()
    submit.call_args.args[0].close()


def test_stop_logs_staged_handoff_refused_by_a_conflicting_gate(
    hub_db: HubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    session_manager = _compact_session_manager(hub_db, {"tmux_pane": "%12"}, source="grok")
    variables = SessionVariableManager(hub_db)
    variables.merge_variables(
        SESSION_ID,
        {
            PENDING_HANDOFF_VARIABLE: {
                "attempt_id": ATTEMPT_ID,
                "clear_session": False,
                "handoff_record_id": "handoff-1",
            },
            HANDOFF_DISPATCH_GATE_VARIABLE: {
                "handoff_staged": True,
                "delivery_pending": True,
                "attempt_id": "b" * 32,
                "clear_session": False,
            },
        },
    )
    event = HookEvent(
        event_type=HookEventType.STOP,
        session_id="provider-session",
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": SESSION_ID},
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        scheduled = terminal_handoff_delivery.schedule_staged_handoff_on_stop(
            event,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            event_loop=None,
        )

    assert scheduled is False
    assert any("gate holds attempt" in warning for warning in _warnings(caplog))
    assert (
        "dispatch_started_at" not in variables.get_variables(SESSION_ID)[PENDING_HANDOFF_VARIABLE]
    )


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
        patch(f"{_COMPACT_DELIVERY}._compact_receipt_exists", return_value=True),
        patch(
            f"{_COMPACT_DELIVERY}._wait_for_compact_boundary",
            new_callable=AsyncMock,
            return_value=None,
        ),
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
    assert ("text", "/compact\n") in runtime.write_log
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE in SessionVariableManager(hub_db).get_variables(
        SESSION_ID
    )

    # Claude restarts in place: SessionStart source=compact on the pre-created row.
    runtime.snapshot_text = f"{'─' * 20}\n❯\xa0\n{'─' * 20}\n"
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
    assert (
        "text",
        f"{build_handoff_continue_prompt()}\n",
    ) in runtime.write_log[compact_writes:]
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in SessionVariableManager(hub_db).get_variables(
        SESSION_ID
    )


def _claimed_compact_attempt(
    hub_db: HubDatabase,
    *,
    found_work: tuple[FoundWorkEntry, ...] = (),
) -> ClaimedHandoffDelivery:
    staged = stage_handoff_attempt(
        hub_db,
        SESSION_ID,
        attempt_id=ATTEMPT_ID,
        handoff=build_handoff_payload(
            current_state="working", next_steps=["continue"], found_work=found_work
        ),
        clear_session=False,
    )
    SessionVariableManager(hub_db).merge_variables(
        SESSION_ID,
        {
            HANDOFF_DISPATCH_GATE_VARIABLE: {
                "handoff_staged": True,
                "delivery_pending": True,
                "attempt_id": ATTEMPT_ID,
                "clear_session": False,
            }
        },
    )
    claimed = claim_staged_handoff_delivery(hub_db, SESSION_ID, ATTEMPT_ID)
    assert claimed is not None
    assert claimed.handoff_record_id == staged.handoff_record_id
    return claimed


@pytest.mark.parametrize(
    "prior_markers,legacy_snapshot",
    [
        ({}, False),
        ({}, True),
        ({FOUND_WORK_VARIABLE: ["previous"], HANDOFF_TURN_END_PENDING_VARIABLE: False}, False),
    ],
)
def test_failed_clear_delivery_restores_all_staged_markers_without_attempt_state(
    hub_db: HubDatabase, prior_markers: dict[str, Any], legacy_snapshot: bool
) -> None:
    session_manager = _compact_session_manager(hub_db, _NATIVE_WORKER_CONTEXT)
    if prior_markers:
        SessionVariableManager(hub_db).merge_variables(SESSION_ID, prior_markers)
        before = SessionVariableManager(hub_db).get_variables(SESSION_ID)
        assert all(before[name] == value for name, value in prior_markers.items())
    staged = stage_clear_attempt(
        hub_db,
        SESSION_ID,
        attempt_id=ATTEMPT_ID,
        handoff=build_handoff_payload(current_state="ready", next_steps=["continue"]),
        terminal_context=_NATIVE_WORKER_CONTEXT,
        chat_context=None,
    )
    claimed = claim_staged_handoff_delivery(
        hub_db, SESSION_ID, ATTEMPT_ID, recover_unarmed_gate=True
    )
    assert claimed is not None
    assert claimed.clear_session is True
    assert claimed.handoff_record_id == staged.handoff_record_id
    assert staged.prior_markers == prior_markers
    pending = SessionVariableManager(hub_db).get_variables(SESSION_ID)[PENDING_HANDOFF_VARIABLE]
    if legacy_snapshot:
        pending.pop("restore_state")
        SessionVariableManager(hub_db).merge_variables(
            SESSION_ID, {PENDING_HANDOFF_VARIABLE: pending}
        )
    else:
        assert pending["restore_state"]["prior_markers"] == prior_markers
    assert (
        SessionVariableManager(hub_db).get_variables(SESSION_ID)[HANDOFF_TURN_END_PENDING_VARIABLE]
        is True
    )

    terminal_handoff_delivery._compensate_delivery_failure(hub_db, claimed, "provider rejected")

    session = session_manager.get(SESSION_ID)
    assert session is not None
    assert session.status == "active"
    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    assert CLEAR_ATTEMPT_VARIABLE not in variables
    assert PENDING_HANDOFF_VARIABLE not in variables
    for name in (HANDOFF_TURN_END_PENDING_VARIABLE, FOUND_WORK_VARIABLE):
        if name in prior_markers:
            assert variables[name] == prior_markers[name]
        else:
            assert name not in variables
    assert variables[FAILED_HANDOFF_VARIABLE]["handoff_record_id"] == staged.handoff_record_id
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE]["delivery_failed"] is True


def test_compact_failure_reads_only_fresh_claude_local_command_stderr(tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    record = {
        "type": "system",
        "subtype": "local_command",
        "content": (
            "<local-command-stderr>Error during compaction: API Error: 500</local-command-stderr>"
        ),
    }
    transcript.write_text(json.dumps(record) + "\n")
    cursor = TranscriptTailCursor.at_eof(transcript)
    assert _fresh_compact_error(cursor) is None

    with transcript.open("a") as stream:
        stream.write(json.dumps(record) + "\n")

    assert _fresh_compact_error(cursor) == "Error during compaction: API Error: 500"


def test_late_compact_failure_cannot_rollback_boundary_receipt(hub_db: HubDatabase) -> None:
    _compact_session_manager(hub_db, _NATIVE_WORKER_CONTEXT)
    claimed = _claimed_compact_attempt(hub_db)
    assert record_handoff_delivery(
        hub_db,
        handoff_id=claimed.handoff_record_id,
        attempt_id=ATTEMPT_ID,
        boundary_kind="compact",
        continuation_session_id=SESSION_ID,
    )

    terminal_handoff_delivery._compensate_delivery_failure(
        hub_db, claimed, "late provider error", error_code="compact_failed"
    )

    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    assert variables[PENDING_HANDOFF_VARIABLE]["attempt_id"] == ATTEMPT_ID
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE]["delivery_pending"] is True
    assert AttentionStateManager(hub_db).get(session_attention_entry_id(SESSION_ID)) is None


@pytest.mark.asyncio
async def test_selected_compact_boundary_receipts_before_timeout_compensation(
    hub_db: HubDatabase,
) -> None:
    _compact_session_manager(hub_db, _NATIVE_WORKER_CONTEXT)
    claimed = _claimed_compact_attempt(hub_db)
    compact_continuation.register_compact_boundary_waiter(
        SESSION_ID, ATTEMPT_ID, claimed.handoff_record_id, _NATIVE_WORKER_CONTEXT
    )
    compact_continuation.arm_compact_boundary_waiter(SESSION_ID, ATTEMPT_ID)
    receipt_selected = threading.Event()
    release_receipt = threading.Event()

    def delayed_receipt(*args: Any, **kwargs: Any) -> bool:
        receipt_selected.set()
        assert release_receipt.wait(timeout=10)
        return record_handoff_delivery(*args, **kwargs)

    with patch.object(compact_continuation, "record_handoff_delivery", side_effect=delayed_receipt):
        notify = asyncio.create_task(
            asyncio.to_thread(
                compact_continuation.notify_compact_boundary,
                hub_db,
                SESSION_ID,
                _NATIVE_WORKER_CONTEXT,
            )
        )
        unregister: asyncio.Task[None] | None = None
        try:
            assert await asyncio.to_thread(receipt_selected.wait, 5)
            unregister = asyncio.create_task(
                asyncio.to_thread(
                    compact_continuation.unregister_compact_boundary_waiter,
                    SESSION_ID,
                    ATTEMPT_ID,
                )
            )
            try:
                await asyncio.wait_for(asyncio.shield(unregister), timeout=0.05)
            except TimeoutError:
                pass
            else:
                terminal_handoff_delivery._compensate_delivery_failure(
                    hub_db, claimed, "timeout before receipt", error_code="compact_failed"
                )
        finally:
            release_receipt.set()
            await notify
            if unregister is not None:
                await unregister

    terminal_handoff_delivery._compensate_delivery_failure(
        hub_db, claimed, "late timeout", error_code="compact_failed"
    )
    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    assert variables[PENDING_HANDOFF_VARIABLE]["attempt_id"] == ATTEMPT_ID
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE]["delivery_pending"] is True
    assert (
        hub_db.fetchone(
            "SELECT 1 FROM session_handoff_deliveries WHERE attempt_id = %s", (ATTEMPT_ID,)
        )
        is not None
    )


@pytest.mark.asyncio
async def test_compact_confirmation_wait_has_a_deadline() -> None:
    waiter = CompactBoundaryWaiter(
        ATTEMPT_ID,
        "handoff-1",
        _NATIVE_WORKER_CONTEXT,
        asyncio.Event(),
        asyncio.get_running_loop(),
        submitted=True,
    )
    pane = SimpleNamespace(snapshot=AsyncMock(return_value="/compact queued"))
    with (
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_CONFIRM_SECONDS", 0.01),
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_POLL_SECONDS", 0.005),
    ):
        failure = await _wait_for_compact_boundary(waiter, pane, "", None)

    assert failure == "compact boundary was not observed before the confirmation deadline"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["claude", "codex"])
async def test_compact_boundary_timeout_does_not_resubmit_without_rejection(
    hub_db: HubDatabase,
    source: str,
) -> None:
    session_manager = _compact_session_manager(hub_db, _NATIVE_WORKER_CONTEXT, source=source)
    claimed = _claimed_compact_attempt(hub_db)
    pane = SimpleNamespace(backend="native", snapshot=AsyncMock(return_value="Compacting..."))
    submissions = 0

    async def send_command(*_args: Any, **kwargs: Any) -> tuple[bool, None, bool, None]:
        nonlocal submissions
        kwargs["on_command_submitting"]()
        kwargs["mark_continuation_pending"]()
        submissions += 1
        return True, None, True, None

    with (
        patch(f"{_COMPACT_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
        patch(f"{_COMPACT_DELIVERY}._interrupt_observer", return_value=(None, None)),
        patch(f"{_COMPACT_DELIVERY}._turn_settled_observer", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._send_terminal_compaction_command", side_effect=send_command),
        patch(f"{_COMPACT_DELIVERY}.composer_reader", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_CONFIRM_SECONDS", 0.01),
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_POLL_SECONDS", 0.005),
        patch(
            "gobby.hooks.terminal_handoff_delivery.shielded_terminal_delivery",
            side_effect=_run_operation,
        ),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )

    assert submissions == 1
    gate = SessionVariableManager(hub_db).get_variables(SESSION_ID)[HANDOFF_DISPATCH_GATE_VARIABLE]
    assert gate["delivery_failed"] is True
    assert gate["delivery_pending"] is False
    assert gate["error_code"] == "compact_unconfirmed"
    assert "get_handoff" in gate["retry_guidance"]
    assert "set_handoff" not in gate["retry_guidance"]
    assert "reconcile_late_compact=true" in gate["recovery_guidance"]
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE in SessionVariableManager(hub_db).get_variables(
        SESSION_ID
    )


def test_late_post_compact_stamps_unconfirmed_attempt_for_recovery(hub_db: HubDatabase) -> None:
    session_manager = _compact_session_manager(hub_db, _NATIVE_WORKER_CONTEXT, source="codex")
    claimed = _claimed_compact_attempt(hub_db)
    terminal_handoff_delivery._compensate_delivery_failure(
        hub_db,
        claimed,
        "compact boundary was not observed before the confirmation deadline",
        error_code="compact_unconfirmed",
    )
    handler = EventHandlers(session_manager=session_manager, agent_run_manager=MagicMock())

    handler.handle_post_compact(
        HookEvent(
            event_type=HookEventType.POST_COMPACT,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={},
            metadata={"_platform_session_id": SESSION_ID},
        )
    )

    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    assert COMPACT_NOTIFICATION_STARTED_AT_VARIABLE in variables


@pytest.mark.asyncio
async def test_compact_boundary_wins_submit_verification_race(hub_db: HubDatabase) -> None:
    session_manager = _compact_session_manager(hub_db, _NATIVE_WORKER_CONTEXT)
    claimed = _claimed_compact_attempt(hub_db)
    pane = SimpleNamespace(backend="native", snapshot=AsyncMock(return_value="Compacting..."))
    submissions: list[str] = []
    handler = EventHandlers(session_manager=session_manager, agent_run_manager=MagicMock())

    async def send_command(*_args: Any, **kwargs: Any) -> tuple[bool, str | None, bool, None]:
        kwargs["on_command_submitting"]()
        kwargs["mark_continuation_pending"]()
        submissions.append(kwargs["cli_source"])
        handler.handle_post_compact(
            HookEvent(
                event_type=HookEventType.POST_COMPACT,
                session_id=SESSION_ID,
                source=SessionSource.CLAUDE,
                timestamp=datetime.now(UTC),
                data={},
                metadata={"_platform_session_id": SESSION_ID},
            )
        )
        return False, "submit verification missed the boundary", True, None

    with (
        patch(f"{_COMPACT_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
        patch(f"{_COMPACT_DELIVERY}._interrupt_observer", return_value=(None, None)),
        patch(f"{_COMPACT_DELIVERY}._turn_settled_observer", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._send_terminal_compaction_command", side_effect=send_command),
        patch(f"{_COMPACT_DELIVERY}.composer_reader", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_CONFIRM_SECONDS", 0.05),
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_POLL_SECONDS", 0.01),
        patch(
            "gobby.hooks.terminal_handoff_delivery.shielded_terminal_delivery",
            side_effect=_run_operation,
        ),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )

    assert submissions == ["claude"]
    handoffs = hub_db.fetchone(
        "SELECT count(*) AS n FROM session_handoffs WHERE session_id = %s", (SESSION_ID,)
    )
    deliveries = hub_db.fetchone(
        "SELECT count(*) AS n FROM session_handoff_deliveries WHERE attempt_id = %s",
        (ATTEMPT_ID,),
    )
    assert handoffs is not None and handoffs["n"] == 1
    assert deliveries is not None and deliveries["n"] == 1


@pytest.mark.asyncio
async def test_queued_compact_waits_for_boundary_before_delivery(hub_db: HubDatabase) -> None:
    session_manager = _compact_session_manager(hub_db, _NATIVE_WORKER_CONTEXT)
    claimed = _claimed_compact_attempt(hub_db)
    pane = SimpleNamespace(backend="native", snapshot=AsyncMock(return_value="/compact queued"))
    submitted = asyncio.Event()
    handler = EventHandlers(session_manager=session_manager, agent_run_manager=MagicMock())

    async def send_command(*_args: Any, **kwargs: Any) -> tuple[bool, None, bool, None]:
        kwargs["on_command_submitting"]()
        kwargs["mark_continuation_pending"]()
        submitted.set()
        return True, None, True, None

    with (
        patch(f"{_COMPACT_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
        patch(f"{_COMPACT_DELIVERY}._interrupt_observer", return_value=(None, None)),
        patch(f"{_COMPACT_DELIVERY}._turn_settled_observer", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._send_terminal_compaction_command", side_effect=send_command),
        patch(f"{_COMPACT_DELIVERY}.composer_reader", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_CONFIRM_SECONDS", 1.0),
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_POLL_SECONDS", 0.05),
        patch(
            "gobby.hooks.terminal_handoff_delivery.shielded_terminal_delivery",
            side_effect=_run_operation,
        ),
    ):
        delivery = asyncio.create_task(
            terminal_handoff_delivery._settle_delivery(
                claimed,
                session_manager=session_manager,
                agent_run_manager=MagicMock(),
                terminal_manager=None,
                terminal_runtime_registry=None,
            )
        )
        await asyncio.wait_for(submitted.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not delivery.done()
        assert (
            hub_db.fetchone(
                "SELECT 1 FROM session_handoff_deliveries WHERE attempt_id = %s", (ATTEMPT_ID,)
            )
            is None
        )
        handler.handle_post_compact(
            HookEvent(
                event_type=HookEventType.POST_COMPACT,
                session_id=SESSION_ID,
                source=SessionSource.CLAUDE,
                timestamp=datetime.now(UTC),
                data={},
                metadata={"_platform_session_id": SESSION_ID},
            )
        )
        await asyncio.wait_for(delivery, timeout=1)

    deliveries = hub_db.fetchone(
        "SELECT count(*) AS n FROM session_handoff_deliveries WHERE attempt_id = %s",
        (ATTEMPT_ID,),
    )
    assert deliveries is not None and deliveries["n"] == 1


@pytest.mark.asyncio
async def test_compact_provider_failure_settles_without_resubmission(hub_db: HubDatabase) -> None:
    session_manager = _compact_session_manager(hub_db, _NATIVE_WORKER_CONTEXT)
    claimed = _claimed_compact_attempt(hub_db)
    output = ""
    pane = SimpleNamespace(backend="native", snapshot=AsyncMock(side_effect=lambda *_a: output))
    submissions = 0

    async def send_command(*_args: Any, **kwargs: Any) -> tuple[bool, None, bool, None]:
        nonlocal output, submissions
        kwargs["on_command_submitting"]()
        kwargs["mark_continuation_pending"]()
        submissions += 1
        output += f"\nattempt {submissions}\nError during compaction: API Error: 500"
        return True, None, True, None

    with (
        patch(f"{_COMPACT_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
        patch(f"{_COMPACT_DELIVERY}._interrupt_observer", return_value=(None, None)),
        patch(f"{_COMPACT_DELIVERY}._turn_settled_observer", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._send_terminal_compaction_command", side_effect=send_command),
        patch(f"{_COMPACT_DELIVERY}.composer_reader", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_CONFIRM_SECONDS", 0.05),
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_POLL_SECONDS", 0.01),
        patch(
            "gobby.hooks.terminal_handoff_delivery.shielded_terminal_delivery",
            side_effect=_run_operation,
        ),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )

    assert submissions == 1
    gate = SessionVariableManager(hub_db).get_variables(SESSION_ID)[HANDOFF_DISPATCH_GATE_VARIABLE]
    assert gate["delivery_failed"] is True
    assert gate["delivery_pending"] is False
    assert "API Error: 500" in gate["reason"]
    assert gate["retry_guidance"]
    attention = AttentionStateManager(hub_db).get(session_attention_entry_id(SESSION_ID))
    assert attention is not None
    assert attention.state == "blocked"
    assert attention.reason == "handoff_delivery_failed"
    assert (
        hub_db.fetchone(
            "SELECT 1 FROM session_handoff_deliveries WHERE attempt_id = %s", (ATTEMPT_ID,)
        )
        is None
    )


async def test_held_compact_failed_interrupt_preserves_undelivered_payload(
    hub_db: HubDatabase,
) -> None:
    session_manager = _compact_session_manager(hub_db, _NATIVE_WORKER_CONTEXT)
    found_work = FoundWorkEntry(
        finding="Terminal capture outage", disposition="escalated", ref="gobby#14531"
    )
    claimed = _claimed_compact_attempt(hub_db, found_work=(found_work,))
    pane = SimpleNamespace(backend="native", snapshot=AsyncMock(return_value="/compact"))
    reason = "CLI did not confirm interruption after 3 attempts"
    send_command = AsyncMock(return_value=(False, reason, False, {"interrupted": False}))

    with (
        patch(f"{_COMPACT_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
        patch(f"{_COMPACT_DELIVERY}._interrupt_observer", return_value=(None, None)),
        patch(f"{_COMPACT_DELIVERY}._turn_settled_observer", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._send_terminal_compaction_command", send_command),
        patch(f"{_COMPACT_DELIVERY}.composer_reader", return_value=None),
        patch(
            "gobby.hooks.terminal_handoff_delivery.shielded_terminal_delivery",
            side_effect=_run_operation,
        ),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )

    send_command.assert_awaited_once()
    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE]["reason"] == reason
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE]["delivery_state"] == "failed_not_deliverable"
    assert "failed_attempt_id" in variables[HANDOFF_DISPATCH_GATE_VARIABLE]["recovery_guidance"]
    assert variables[FAILED_HANDOFF_VARIABLE] == {
        "attempt_id": ATTEMPT_ID,
        "handoff_record_id": claimed.handoff_record_id,
        "delivery_state": "failed_not_deliverable",
        "found_work": [found_work.as_dict()],
    }
    assert PENDING_HANDOFF_VARIABLE not in variables
    assert (
        hub_db.fetchone(
            "SELECT 1 FROM session_handoffs WHERE id = %s", (claimed.handoff_record_id,)
        )
        is not None
    )
    assert (
        hub_db.fetchone(
            "SELECT 1 FROM session_handoff_deliveries WHERE attempt_id = %s", (ATTEMPT_ID,)
        )
        is None
    )
    assert claim_staged_handoff_delivery(hub_db, SESSION_ID, ATTEMPT_ID) is None


async def test_tmux_pane_only_session_gets_no_continuation(
    hub_db: HubDatabase,
) -> None:
    """Continuations are native-only; a session bound just to a tmux pane has no delivery path."""
    session_manager = _compact_session_manager(hub_db, {"tmux_pane": "%12"})
    assert mark_handoff_compact_continuation_pending(hub_db, SESSION_ID, attempt_id=ATTEMPT_ID)
    runtime = FakeRuntime(backend="native")
    handler = _session_start_handler(
        session_manager, MemoryTerminalStore(), runtime_registry(runtime)
    )

    scheduled = _consume_pending_handoff_compact_continuation(
        handler,
        session_source="compact",
        pending_session_id=SESSION_ID,
        target_session=session_manager.get(SESSION_ID),
    )
    await _await_continuations()

    assert scheduled is False
    assert runtime.write_log == []


@pytest.mark.parametrize(
    ("dispatch_owner", "recovers"),
    [(DISPATCH_OWNER, False), ("dead-daemon-owner", True)],
    ids=["dispatched-by-this-daemon", "abandoned-by-a-dead-daemon"],
)
def test_stop_recovers_only_a_dispatch_this_daemon_does_not_own(
    hub_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
    dispatch_owner: str,
    recovers: bool,
) -> None:
    session_manager = _compact_session_manager(hub_db, {"tmux_pane": "%12"}, source="grok")
    variables = SessionVariableManager(hub_db)
    started_at = datetime.now(UTC).isoformat()
    variables.merge_variables(
        SESSION_ID,
        {
            PENDING_HANDOFF_VARIABLE: {
                "attempt_id": ATTEMPT_ID,
                "clear_session": False,
                "handoff_record_id": "22222222-2222-4222-8222-222222222222",
                "created_at": started_at,
                "dispatch_started_at": started_at,
                "dispatch_owner": dispatch_owner,
            },
            HANDOFF_DISPATCH_GATE_VARIABLE: {
                "handoff_staged": True,
                "delivery_pending": True,
                "attempt_id": ATTEMPT_ID,
                "clear_session": False,
            },
        },
    )
    event_loop = MagicMock()
    event_loop.is_closed.return_value = False
    event = HookEvent(
        event_type=HookEventType.STOP,
        session_id="provider-session",
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": SESSION_ID},
    )

    with (
        caplog.at_level(logging.WARNING, logger=LOGGER_NAME),
        patch("gobby.hooks.terminal_handoff_delivery._settle_delivery", new_callable=AsyncMock),
        patch("gobby.hooks.terminal_handoff_delivery.asyncio.run_coroutine_threadsafe") as submit,
    ):
        scheduled = terminal_handoff_delivery.schedule_staged_handoff_on_stop(
            event,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            event_loop=event_loop,
        )

    marker = variables.get_variables(SESSION_ID)[PENDING_HANDOFF_VARIABLE]
    assert scheduled is recovers
    assert _warnings(caplog) == []
    assert submit.call_count == int(recovers)
    if recovers:
        assert marker["dispatch_owner"] == DISPATCH_OWNER
        submit.call_args.args[0].close()
    else:
        assert marker["dispatch_owner"] == dispatch_owner
        assert marker["dispatch_started_at"] == started_at


@pytest.mark.asyncio
async def test_continuation_resubmits_until_before_agent_observed(
    hub_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A false 'draft left' read keeps re-submitting until BEFORE_AGENT arrives.

    The 22:28:40 CDT read on 2026-09-21 reported the composer left the draft while
    the prompt was still unsent. Only the session's BEFORE_AGENT (a turn-lifecycle
    generation bump) settles the continuation, and no retry may follow it.
    """
    from gobby.agents.idle_detector import ComposerRead
    from gobby.sessions import continuation_retry
    from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer

    session_manager = _compact_session_manager(hub_db, {"tmux_pane": "%12"})
    pane = SimpleNamespace(
        backend="native",
        snapshot=AsyncMock(return_value=""),
        type_text=AsyncMock(return_value=(True, None)),
    )

    # The first (entry) read is the false positive: it reports the draft left while
    # the prompt never reached the CLI. The read after the bare Enter correctly
    # shows the prompt still held, so the bounded budget re-sends Enter.
    entry_reads = {"count": 0}

    def composer_read(_snapshot: str | None) -> ComposerRead:
        entry_reads["count"] += 1
        if entry_reads["count"] == 1:
            return ComposerRead(state="empty")
        return ComposerRead(state="draft", line="continue the handoff")

    resent_enters: list[str] = []

    async def fake_send_key(pane: Any, key: str, session_id: str, *, action: str) -> Any:
        resent_enters.append(key)
        return True, None

    recorded_attempts: list[int] = []

    async def fake_await_before_agent(
        db: HubDatabase, session_id: str, *, baseline_generation: int | None, **kwargs: Any
    ) -> bool:
        recorded_attempts.append(len(recorded_attempts) + 1)
        # The BEFORE_AGENT arrives once the second Enter has been sent.
        return len(resent_enters) >= 2

    with (
        caplog.at_level(logging.WARNING, logger="gobby.sessions.continuation_retry"),
        patch.object(continuation_retry, "send_pane_key", fake_send_key),
        patch.object(continuation_retry, "await_before_agent", fake_await_before_agent),
    ):
        confirmed = await continuation_retry.resubmit_until_before_agent(
            pane,
            "continue the handoff",
            SESSION_ID,
            db=hub_db,
            baseline_generation=7,
            cli_source="claude",
            composer_read=composer_read,
            verify_seconds=0.0,
            retry_limit=3,
        )

    assert confirmed is True
    # Two re-submits happened before the hook arrived, and no retry after it.
    assert len(resent_enters) == 2
    # Retries are bounded, not logged (daemon-loop logging ban, #23303).
    assert caplog.records == []
    # Every check before the arrival returned False; the loop never re-submitted
    # past the confirmed hook.
    assert len(recorded_attempts) >= 3
    # The turn-lifecycle reducer is what the confirmation reads; a bump past the
    # baseline is what proves BEFORE_AGENT for this session.
    reducer = TurnLifecycleReducer(session_manager)
    assert continuation_retry.turn_lifecycle_generation(hub_db, SESSION_ID) == 0
    reducer.begin_turn(SESSION_ID, TurnEvidence(source="claude"))
    assert continuation_retry.turn_lifecycle_generation(hub_db, SESSION_ID) == 1


@pytest.mark.asyncio
async def test_continuation_does_not_repaste_when_enter_delivered_before_agent(
    hub_db: HubDatabase,
) -> None:
    """An Enter that delivered BEFORE_AGENT must not be followed by a re-paste.

    The entry read can report the draft gone while the composer still holds it, so
    the retry sends Enter first. That Enter submits the held draft and the
    session's BEFORE_AGENT arrives immediately; the composer is then empty, which
    the next read cannot distinguish from a lost prompt. Re-pasting would queue a
    duplicate continuation, so the hook is rechecked after the Enter and the
    re-paste is skipped (#22706 MEDIUM).
    """
    from gobby.agents.idle_detector import ComposerRead
    from gobby.sessions import continuation_retry

    class _Pane:
        backend = "native"

        def __init__(self) -> None:
            self.typed: list[str] = []
            self.keys: list[str] = []

        async def snapshot(self, *_args: Any, **_kwargs: Any) -> str:
            return ""

        async def type_text(self, text: str) -> tuple[bool, str | None]:
            self.typed.append(text)
            return True, None

        async def send_key(self, key: str) -> tuple[bool, str | None]:
            self.keys.append(key)
            return True, None

    def composer_read(_snapshot: str | None) -> ComposerRead:
        # The false positive: it reports the draft left while it is still held.
        return ComposerRead(state="empty")

    async def hook_after_enter() -> bool:
        # The Enter in resubmit_continuation submitted the held draft, so the
        # session's BEFORE_AGENT has already arrived by the time this runs.
        return True

    async def hook_not_arrived() -> bool:
        return False

    pane: Any = _Pane()
    resent = await continuation_retry.resubmit_continuation(
        pane,
        "continue the handoff",
        SESSION_ID,
        cli_source="claude",
        composer_read=composer_read,
        verify_seconds=0.0,
        before_agent_check=hook_after_enter,
    )

    assert resent is True
    assert pane.keys == ["enter"]
    assert pane.typed == [], "a delivered BEFORE_AGENT must not be re-pasted"

    # Without the hook the same inputs do re-paste the lost copy, so the check
    # above is what prevents the duplicate rather than the composer state.
    pane_without_hook: Any = _Pane()
    await continuation_retry.resubmit_continuation(
        pane_without_hook,
        "continue the handoff",
        SESSION_ID,
        cli_source="claude",
        composer_read=composer_read,
        verify_seconds=0.0,
        before_agent_check=hook_not_arrived,
    )
    assert pane_without_hook.typed == ["continue the handoff\n"]


async def test_continuation_repastes_the_prompt_when_the_composer_is_empty() -> None:
    """An empty composer is positive evidence the prompt was lost, so it is re-typed.

    ``resubmit_continuation`` used to send a bare Enter only, which cannot recover a
    prompt the false-positive read consumed. With an ``empty`` read the text is
    re-pasted and verified (#22706 MEDIUM).
    """
    from gobby.agents.idle_detector import ComposerRead
    from gobby.sessions import continuation_retry

    class _Pane:
        backend = "native"

        def __init__(self) -> None:
            self.typed: list[str] = []
            self.keys: list[str] = []

        async def snapshot(self, *_args: Any, **_kwargs: Any) -> str:
            return ""

        async def type_text(self, text: str) -> tuple[bool, str | None]:
            self.typed.append(text)
            return True, None

        async def send_key(self, key: str) -> tuple[bool, str | None]:
            self.keys.append(key)
            return True, None

    def composer_read(_snapshot: str | None) -> ComposerRead:
        return ComposerRead(state="empty")

    pane: Any = _Pane()
    resent = await continuation_retry.resubmit_continuation(
        pane,
        "continue the handoff",
        SESSION_ID,
        cli_source="claude",
        composer_read=composer_read,
        verify_seconds=0.0,
    )

    assert resent is True
    # The lost prompt was re-typed (with its newline) exactly once.
    assert pane.typed == ["continue the handoff\n"]
    # One bare Enter at entry, then submit_text's own Enter after the paste.
    assert pane.keys == ["enter", "enter"]


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(ComposerRead(state="unknown"), id="unreadable"),
        pytest.param(ComposerRead(state="draft", line="half-typed operator note"), id="draft"),
    ],
)
async def test_continuation_never_writes_over_unknown_or_operator_text(
    read: ComposerRead,
) -> None:
    """A frame that may hold operator text gets no Enter, drain or re-paste.

    #22915 gates every composer write on a confirmed-empty read: an operator draft
    belongs to the operator, and an unclassifiable frame may hide one. The retry
    writes nothing and reports False, so the caller's durable fallback delivers
    the continuation instead.
    """
    from gobby.sessions import continuation_retry

    class _Pane:
        backend = "native"

        def __init__(self) -> None:
            self.typed: list[str] = []
            self.keys: list[str] = []

        async def snapshot(self, *_args: Any, **_kwargs: Any) -> str:
            return ""

        async def type_text(self, text: str) -> tuple[bool, str | None]:
            self.typed.append(text)
            return True, None

        async def send_key(self, key: str) -> tuple[bool, str | None]:
            self.keys.append(key)
            return True, None

    def composer_read(_snapshot: str | None) -> ComposerRead:
        return read

    pane: Any = _Pane()
    resent = await continuation_retry.resubmit_continuation(
        pane,
        "continue the handoff",
        SESSION_ID,
        cli_source="claude",
        composer_read=composer_read,
        verify_seconds=0.0,
    )

    assert resent is False
    assert pane.typed == []
    assert pane.keys == []


async def test_continuation_without_a_composer_reader_only_resends_enter() -> None:
    """With no reader to verify a paste, a bare Enter is the only safe retry.

    Enter submits a held copy and is a no-op on an empty composer; a re-paste
    nothing can verify could only duplicate the prompt.
    """
    from gobby.sessions import continuation_retry

    class _Pane:
        backend = "native"

        def __init__(self) -> None:
            self.typed: list[str] = []
            self.keys: list[str] = []

        async def type_text(self, text: str) -> tuple[bool, str | None]:
            self.typed.append(text)
            return True, None

        async def send_key(self, key: str) -> tuple[bool, str | None]:
            self.keys.append(key)
            return True, None

    pane: Any = _Pane()
    resent = await continuation_retry.resubmit_continuation(
        pane,
        "continue the handoff",
        SESSION_ID,
        cli_source="claude",
        composer_read=None,
        verify_seconds=0.0,
    )

    assert resent is True
    assert pane.keys == ["enter"]
    assert pane.typed == []
