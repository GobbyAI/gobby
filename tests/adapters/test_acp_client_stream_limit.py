"""Regression tests for ACP stdout handling of oversized JSON-RPC frames.

asyncio's subprocess ``StreamReader`` defaults to a 64 KiB buffer, so a single
JSON-RPC line larger than that raised ``LimitOverrunError`` out of
``readline()`` and killed the ACP session (observed live on Grok, but it
affects every ACP CLI on any large message). ``ACPClient`` now widens the
subprocess reader limit; these tests pin that behavior.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.adapters import acp_client
from gobby.adapters.acp_client import ACP_STREAM_READER_LIMIT_BYTES, ACPClient

pytestmark = pytest.mark.unit

# 256 KiB — comfortably past asyncio's 64 KiB default readline limit.
_OVERSIZED_TEXT = "x" * (256 * 1024)


class _StubACPClient(ACPClient):
    cli_name = "stub-acp"
    display_name = "StubACP"
    prompt_timeout_env = "GOBBY_STUB_ACP_PROMPT_TIMEOUT_SECONDS"


def _reader_with_lines(limit: int, *lines: str) -> asyncio.StreamReader:
    reader = asyncio.StreamReader(limit=limit)
    for line in lines:
        reader.feed_data(line.encode())
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_read_stream_handles_line_larger_than_default_limit() -> None:
    """A single JSON-RPC line >64 KiB is parsed instead of crashing the stream."""
    notification = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": [{"type": "text", "text": _OVERSIZED_TEXT}],
                    }
                },
            }
        )
        + "\n"
    )
    final = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"stats": {}}}) + "\n"
    assert len(notification.encode()) > 64 * 1024

    client = _StubACPClient(cli_path="/stub")
    client._process = SimpleNamespace(  # type: ignore[assignment]
        stdout=_reader_with_lines(ACP_STREAM_READER_LIMIT_BYTES, notification, final)
    )
    client._prompt_timeout = 5.0

    events = [event async for event in client._read_stream(expected_response_id=1)]

    # The oversized assistant chunk survived intact, followed by the final
    # result marker — no LimitOverrunError.
    assert len(events) == 2
    assert events[0].event_type == "content_delta"
    assert events[0].data["content"] == _OVERSIZED_TEXT
    assert events[-1].event_type == "result"


@pytest.mark.asyncio
async def test_default_stream_limit_overflows_on_oversized_line() -> None:
    """Documents the bug: asyncio's 64 KiB default reader raises on the same line."""
    reader = _reader_with_lines(64 * 1024, _OVERSIZED_TEXT + "\n")

    # LimitOverrunError is a subclass of ValueError.
    with pytest.raises(ValueError):
        await reader.readline()


@pytest.mark.asyncio
async def test_start_widens_subprocess_stream_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """start() wires the widened reader limit into create_subprocess_exec."""
    captured: dict[str, Any] = {}

    class _Sentinel(RuntimeError):
        """Aborts start() right after the subprocess-launch call is inspected."""

    async def fake_create_subprocess_exec(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise _Sentinel

    monkeypatch.setattr(acp_client.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    client = _StubACPClient(cli_path="/stub")
    monkeypatch.setattr(client, "_build_launch_command", lambda *a, **k: ["/stub", "--acp"])

    with pytest.raises(_Sentinel):
        await client.start()

    assert captured["limit"] == ACP_STREAM_READER_LIMIT_BYTES
