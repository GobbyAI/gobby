"""Tests for the Droid web-chat backend."""

from __future__ import annotations

import inspect
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.llm.claude_models import DoneEvent, TextChunk, ThinkingEvent
from gobby.servers.websocket.chat.backends.droid import (
    DroidManagedChatSession,
    DroidWebChatBackend,
    _droid_tool_name_adapter,
    _parse_droid_stream_line,
)

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path("tests/fixtures/droid/stream_json")


class _FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [(line + "\n").encode("utf-8") for line in lines]

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, lines: list[str]) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.terminated = False
        self.killed = False

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _fixture_lines(name: str) -> list[str]:
    return FIXTURE_DIR.joinpath(name).read_text(encoding="utf-8").splitlines()


def test_droid_tool_name_adapter() -> None:
    assert _droid_tool_name_adapter("gobby___list_mcp_servers") == (
        "mcp__gobby__list_mcp_servers"
    )
    assert _droid_tool_name_adapter("mcp__gobby__list") == "mcp__gobby__list"
    assert _droid_tool_name_adapter("Read") == "Read"


def test_parse_stream_json_normalizes_content_blocks() -> None:
    events = []
    for line in _fixture_lines("tool_call.jsonl"):
        events.extend(_parse_droid_stream_line(line))

    assert [event.event_type for event in events] == ["content_delta", "content_delta", "result"]
    assert events[0].data["kind"] == "tool_use"
    assert events[0].data["tool_name"] == "gobby___list_mcp_servers"
    assert events[1].data["kind"] == "tool_result"


@pytest.mark.asyncio
async def test_managed_session_translates_text_thinking_and_done() -> None:
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)
    session._connected = True

    async def fake_send_message(_session: Any, _prompt: str):
        for line in _fixture_lines("thinking.jsonl"):
            for event in _parse_droid_stream_line(line):
                yield event

    backend.send_message = fake_send_message  # type: ignore[method-assign]

    events = [event async for event in session.send_message("hello")]

    assert any(isinstance(event, ThinkingEvent) for event in events)
    assert [event.content for event in events if isinstance(event, TextChunk)] == ["Done"]
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_send_message_streams_fixture_and_writes_prompt() -> None:
    process = _FakeProcess(_fixture_lines("text_response.jsonl"))
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)
    session.project_path = "/tmp/project"

    with (
        patch("gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ) as create_process,
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = [event async for event in session.send_message("hello")]

    create_process.assert_called_once()
    command = create_process.call_args.args
    assert command[:4] == ("/bin/droid", "exec", "--input-format", "stream-json")
    assert "--model" in command
    assert process.stdin.writes
    assert [event.content for event in events if isinstance(event, TextChunk)] == [
        "Hello from Droid"
    ]
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_eof_before_result_yields_error_event() -> None:
    process = _FakeProcess(_fixture_lines("eof.jsonl"))
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)

    with (
        patch("gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session)
        events = [event async for event in session.send_message("hello")]

    assert any(isinstance(event, TextChunk) and "Droid stream ended" in event.content for event in events)
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_switch_model_respawns_session_object_first() -> None:
    processes = [
        _FakeProcess(_fixture_lines("session_init.jsonl")),
        _FakeProcess(_fixture_lines("session_init.jsonl")),
    ]
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)

    with (
        patch("gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            side_effect=processes,
        ) as create_process,
    ):
        await backend.attach_session(session, model="gpt-5.4")
        await backend.switch_model(session, "gpt-5.4-mini")

    assert create_process.call_count == 2
    assert processes[0].terminated is True
    assert session._model == "gpt-5.4-mini"


def test_public_backend_methods_use_session_object_first_contract() -> None:
    for method_name in ("attach_session", "detach_session", "send_message", "switch_model"):
        params = list(inspect.signature(getattr(DroidWebChatBackend, method_name)).parameters)
        assert params[:2] == ["self", "session"]


@pytest.mark.asyncio
async def test_health_reports_missing_droid() -> None:
    backend = DroidWebChatBackend()

    with patch("gobby.servers.websocket.chat.backends.droid.shutil.which", return_value=None):
        await backend.start()

    health = backend.health()
    assert health.available is False
    assert health.startup_error == "droid CLI not found in PATH"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_droid_exec_binary_starts(tmp_path: Path) -> None:
    droid = shutil.which("droid")
    if droid is None:
        pytest.skip("droid CLI not installed")

    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)
    session.project_path = str(tmp_path)

    await backend.attach_session(session)
    await backend.detach_session(session)
