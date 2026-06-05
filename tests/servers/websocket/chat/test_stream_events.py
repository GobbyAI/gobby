"""Tests for ChatStreamEventHandler tool-event reconciliation.

Focus: out-of-order ACP delivery where a ToolResultEvent arrives before its
ToolCallEvent. The handler must resolve the real tool name instead of leaving
the UI showing an "unknown" tool.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.llm.claude_models import DoneEvent, ToolCallEvent, ToolResultEvent
from gobby.servers.websocket.chat._stream_events import (
    ChatStreamEventHandler,
    ChatStreamEventState,
)
from gobby.servers.websocket.chat.content_blocks import AssistantContentBlocks

pytestmark = pytest.mark.unit


class _FakeTransport:
    """Records every frame the handler broadcasts."""

    def __init__(self, fail_on_status: str | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_on_status = fail_on_status

    def base_msg(self, **fields: Any) -> dict[str, Any]:
        return dict(fields)

    async def safe_send(self, msg: dict[str, Any]) -> bool:
        self.sent.append(msg)
        return msg.get("status") != self.fail_on_status


class _FakePersistence:
    """Minimal persistence stub for the done path."""

    def __init__(self) -> None:
        self.persisted = False

    async def persist_current_assistant(self, session: Any) -> None:
        self.persisted = True

    def session_ref(self) -> str | None:
        return None

    async def persist_sdk_session_id(self, session: Any, sdk_sid: str | None) -> None:
        return None

    async def persist_done_metadata(self, session: Any, event: Any) -> None:
        return None


def _make_handler(
    transport: _FakeTransport,
    blocks: AssistantContentBlocks,
    persistence: _FakePersistence | None = None,
) -> ChatStreamEventHandler:
    return ChatStreamEventHandler(
        SimpleNamespace(),
        "conv-1",
        transport,
        persistence or _FakePersistence(),
        blocks,
        ChatStreamEventState(assistant_message_id="assistant-1"),
        None,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_result_before_tool_call_resolves_real_name() -> None:
    """A result that beats its call is buffered, then reconciled with the real name."""
    transport = _FakeTransport()
    blocks = AssistantContentBlocks()
    handler = _make_handler(transport, blocks)

    # Result arrives first (out-of-order ACP delivery).
    await handler._handle_tool_result(
        ToolResultEvent(tool_call_id="call-1", success=True, result={"content": "ok"})
    )

    # Nothing emitted yet — buffered, no provisional block, no "unknown" frame.
    assert transport.sent == []
    assert "call-1" in handler.state.orphan_tool_results
    assert blocks.blocks == []

    # The matching call lands.
    await handler._handle_tool_call(
        ToolCallEvent(
            tool_call_id="call-1",
            tool_name="Bash",
            server_name="builtin",
            arguments={"command": "pwd"},
        )
    )

    # Buffer drained; UI saw calling -> completed in order with the real name.
    assert handler.state.orphan_tool_results == {}
    assert transport.sent[0]["status"] == "calling"
    assert transport.sent[0]["tool_name"] == "Bash"
    assert transport.sent[1]["status"] == "completed"
    assert transport.sent[1]["result"] == {"content": "ok"}

    tool_call = blocks.blocks[0]["tool_calls"][0]
    assert tool_call["tool_name"] == "Bash"
    assert tool_call["status"] == "completed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_call_then_result_in_order_unchanged() -> None:
    """In-order delivery still completes immediately and never buffers."""
    transport = _FakeTransport()
    blocks = AssistantContentBlocks()
    handler = _make_handler(transport, blocks)

    await handler._handle_tool_call(
        ToolCallEvent(
            tool_call_id="call-2",
            tool_name="Read",
            server_name="builtin",
            arguments={"file_path": "x"},
        )
    )
    await handler._handle_tool_result(
        ToolResultEvent(tool_call_id="call-2", success=True, result={"content": "data"})
    )

    assert handler.state.orphan_tool_results == {}
    assert transport.sent[0]["status"] == "calling"
    assert transport.sent[0]["tool_name"] == "Read"
    assert transport.sent[-1]["status"] == "completed"

    tool_call = blocks.blocks[0]["tool_calls"][0]
    assert tool_call["tool_name"] == "Read"
    assert tool_call["status"] == "completed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_result_before_call_resolves_error_status() -> None:
    """Out-of-order failures reconcile to the error status with the real name."""
    transport = _FakeTransport()
    blocks = AssistantContentBlocks()
    handler = _make_handler(transport, blocks)

    await handler._handle_tool_result(
        ToolResultEvent(tool_call_id="call-3", success=False, error="boom")
    )
    await handler._handle_tool_call(
        ToolCallEvent(
            tool_call_id="call-3",
            tool_name="Edit",
            server_name="builtin",
            arguments={"file_path": "y"},
        )
    )

    assert transport.sent[1]["status"] == "error"
    assert transport.sent[1]["error"] == "boom"
    tool_call = blocks.blocks[0]["tool_calls"][0]
    assert tool_call["tool_name"] == "Edit"
    assert tool_call["status"] == "error"
    assert tool_call["error"] == "boom"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orphan_result_flushed_as_provisional_on_done(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A result whose call never arrives is surfaced provisionally at stream end."""
    transport = _FakeTransport()
    blocks = AssistantContentBlocks()
    persistence = _FakePersistence()
    handler = _make_handler(transport, blocks, persistence)

    await handler._handle_tool_result(
        ToolResultEvent(tool_call_id="orphan-1", success=True, result={"content": "x"})
    )
    assert blocks.blocks == []

    with caplog.at_level(logging.INFO, logger="gobby.servers.websocket.chat._stream_events"):
        await handler._handle_done(DoneEvent(tool_calls_count=0), SimpleNamespace())

    # Provisional tool call created, completed, and the buffer cleared before persist.
    assert handler.state.orphan_tool_results == {}
    assert persistence.persisted is True
    tool_call = blocks.blocks[0]["tool_calls"][0]
    assert tool_call["tool_name"] == "unknown"
    assert tool_call["status"] == "completed"
    record = next(
        item
        for item in caplog.records
        if item.message == "Flushing orphan ToolResultEvent as unknown tool call"
    )
    assert record.call_id == "orphan-1"
    assert record.tool_name == "unknown"
    assert record.server_name == "unknown"
    assert record.result_type == "dict"
    assert record.result_length == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orphan_result_retained_when_terminal_send_fails() -> None:
    """A failed terminal send must not discard the buffered tool result."""
    transport = _FakeTransport(fail_on_status="completed")
    blocks = AssistantContentBlocks()
    persistence = _FakePersistence()
    handler = _make_handler(transport, blocks, persistence)
    event = ToolResultEvent(tool_call_id="orphan-2", success=True, result={"content": "x"})

    await handler._handle_tool_result(event)
    await handler._handle_done(DoneEvent(tool_calls_count=0), SimpleNamespace())

    assert handler.state.orphan_tool_results == {"orphan-2": event}
    assert persistence.persisted is True
    assert [msg["status"] for msg in transport.sent if msg["type"] == "tool_status"] == [
        "calling",
        "completed",
    ]
