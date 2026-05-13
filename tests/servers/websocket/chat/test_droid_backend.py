"""Tests for the Droid web-chat backend."""

from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.llm.claude_models import DoneEvent, TextChunk, ThinkingEvent
from gobby.servers.websocket.chat.droid_backend import (
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


def _session_init_line(model: str = "gpt-5.4") -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "type": "response",
            "factoryApiVersion": "1.0.0",
            "factoryProtocolVersion": "1.25.0",
            "id": "gobby-init-1",
            "result": {
                "sessionId": "droid-session-1",
                "session": {"messages": []},
                "settings": {"modelId": model, "reasoningEffort": "high"},
            },
        }
    )


def _turn_response_lines(text: str) -> list[str]:
    return [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "type": "response",
                "factoryApiVersion": "1.0.0",
                "factoryProtocolVersion": "1.25.0",
                "id": "gobby-message-2",
                "result": {},
            }
        ),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "type": "notification",
                "factoryApiVersion": "1.0.0",
                "factoryProtocolVersion": "1.25.0",
                "method": "droid.session_notification",
                "params": {
                    "notification": {
                        "type": "assistant_text_delta",
                        "messageId": "msg-1",
                        "blockIndex": 0,
                        "textDelta": text,
                    }
                },
            }
        ),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "type": "notification",
                "factoryApiVersion": "1.0.0",
                "factoryProtocolVersion": "1.25.0",
                "method": "droid.session_notification",
                "params": {
                    "notification": {
                        "type": "droid_working_state_changed",
                        "newState": "idle",
                    }
                },
            }
        ),
    ]


def _permission_request_line(
    *,
    request_id: str = "permission-1",
    tool_id: str = "tool-1",
    tool_name: str = "Read",
    tool_input: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "type": "request",
            "factoryApiVersion": "1.0.0",
            "factoryProtocolVersion": "1.25.0",
            "id": request_id,
            "method": "droid.request_permission",
            "params": {
                "toolUses": [
                    {
                        "toolUse": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": tool_input
                            if tool_input is not None
                            else {"file_path": "README.md"},
                        },
                        "confirmationType": "read",
                        "details": {"type": "read", "filePath": "README.md"},
                    }
                ],
                "options": [
                    {"label": "Proceed once", "value": "proceed_once"},
                    {"label": "Cancel", "value": "cancel"},
                ],
            },
        }
    )


def test_droid_tool_name_adapter() -> None:
    assert _droid_tool_name_adapter("gobby___list_mcp_servers") == ("mcp__gobby__list_mcp_servers")
    assert _droid_tool_name_adapter("mcp__gobby__list") == "mcp__gobby__list"
    assert _droid_tool_name_adapter("Execute") == "Bash"
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
    session._model = "gpt-5.4"
    session._context_window_overrides = {"gpt-5.4": 200_000}

    async def fake_send_message(_session: Any, _prompt: str):
        for line in _fixture_lines("thinking.jsonl"):
            for event in _parse_droid_stream_line(line):
                yield event

    backend.send_message = fake_send_message

    events = [event async for event in session.send_message("hello")]

    assert any(isinstance(event, ThinkingEvent) for event in events)
    assert [event.content for event in events if isinstance(event, TextChunk)] == ["Done"]
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].context_window == 200_000


@pytest.mark.asyncio
async def test_send_message_streams_fixture_and_writes_prompt() -> None:
    process = _FakeProcess(_fixture_lines("text_response.jsonl"))
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)
    session.project_path = "/tmp/project"

    with (
        patch("gobby.servers.websocket.chat.droid_backend.shutil.which", return_value="/bin/droid"),
        patch(
            "gobby.servers.websocket.chat.droid_backend.asyncio.create_subprocess_exec",
            return_value=process,
        ) as create_process,
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = [event async for event in session.send_message("hello")]

    create_process.assert_called_once()
    command = create_process.call_args.args
    assert command[:6] == (
        "/bin/droid",
        "exec",
        "--input-format",
        "stream-jsonrpc",
        "--output-format",
        "stream-jsonrpc",
    )
    assert "--model" in command
    assert process.stdin.writes
    writes = [json.loads(write.decode("utf-8")) for write in process.stdin.writes]
    assert writes[0]["method"] == "droid.initialize_session"
    assert writes[0]["params"]["cwd"] == "/tmp/project"
    assert writes[1]["method"] == "droid.add_user_message"
    assert writes[1]["params"]["text"].endswith("hello")
    assert [event.content for event in events if isinstance(event, TextChunk)] == [
        "Hello from Droid"
    ]
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_send_message_answers_auto_allowed_permission_request() -> None:
    process = _FakeProcess(
        [_session_init_line()]
        + [
            _turn_response_lines("Approved")[0],
            _permission_request_line(request_id="permission-1"),
            *_turn_response_lines("Approved")[1:],
        ]
    )
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)
    session.project_path = "/tmp/project"

    with (
        patch("gobby.servers.websocket.chat.droid_backend.shutil.which", return_value="/bin/droid"),
        patch(
            "gobby.servers.websocket.chat.droid_backend.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = [event async for event in session.send_message("read the readme")]

    writes = [json.loads(write.decode("utf-8")) for write in process.stdin.writes]
    permission_response = writes[2]
    assert permission_response["type"] == "response"
    assert permission_response["id"] == "permission-1"
    assert permission_response["result"] == {"selectedOption": "proceed_once"}
    assert [event.content for event in events if isinstance(event, TextChunk)] == ["Approved"]
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_send_message_cancels_permission_when_pre_tool_blocks() -> None:
    process = _FakeProcess(
        [_session_init_line()]
        + [
            _turn_response_lines("Denied")[0],
            _permission_request_line(
                request_id="permission-1",
                tool_id="tool-1",
                tool_name="Execute",
                tool_input={"command": "python -c 'print(1)'"},
            ),
            *_turn_response_lines("Denied")[1:],
        ]
    )
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)
    session.project_path = "/tmp/project"
    pre_tool_calls: list[dict[str, Any]] = []

    async def block_pre_tool(payload: dict[str, Any]) -> dict[str, Any]:
        pre_tool_calls.append(payload)
        return {"decision": "block", "reason": "blocked by test"}

    session._on_pre_tool = block_pre_tool

    with (
        patch("gobby.servers.websocket.chat.droid_backend.shutil.which", return_value="/bin/droid"),
        patch(
            "gobby.servers.websocket.chat.droid_backend.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = [event async for event in session.send_message("run a command")]

    writes = [json.loads(write.decode("utf-8")) for write in process.stdin.writes]
    permission_response = writes[2]
    assert permission_response["id"] == "permission-1"
    assert permission_response["result"] == {"selectedOption": "cancel"}
    assert pre_tool_calls == [
        {"tool_name": "Bash", "tool_input": {"command": "python -c 'print(1)'"}}
    ]
    assert [event.content for event in events if isinstance(event, TextChunk)] == ["Denied"]


@pytest.mark.asyncio
async def test_send_message_waits_for_user_permission_approval() -> None:
    process = _FakeProcess(
        [_session_init_line()]
        + [
            _turn_response_lines("Ran")[0],
            _permission_request_line(
                request_id="permission-1",
                tool_id="tool-1",
                tool_name="Execute",
                tool_input={"command": "python -c 'print(1)'"},
            ),
            *_turn_response_lines("Ran")[1:],
        ]
    )
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)
    session.project_path = "/tmp/project"
    session.chat_mode = "auto"
    approval_calls: list[tuple[str, dict[str, Any]]] = []

    async def approve_tool(tool_name: str, arguments: dict[str, Any]) -> None:
        approval_calls.append((tool_name, arguments))
        session.provide_approval("approve")

    session._tool_approval_callback = approve_tool

    with (
        patch("gobby.servers.websocket.chat.droid_backend.shutil.which", return_value="/bin/droid"),
        patch(
            "gobby.servers.websocket.chat.droid_backend.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = [event async for event in session.send_message("run a command")]

    writes = [json.loads(write.decode("utf-8")) for write in process.stdin.writes]
    permission_response = writes[2]
    assert approval_calls == [("Bash", {"command": "python -c 'print(1)'"})]
    assert permission_response["result"] == {"selectedOption": "proceed_once"}
    assert [event.content for event in events if isinstance(event, TextChunk)] == ["Ran"]


@pytest.mark.asyncio
async def test_send_message_supports_multiple_turns_on_same_process() -> None:
    process = _FakeProcess(
        [_session_init_line()] + _turn_response_lines("First") + _turn_response_lines("Second")
    )
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)
    session.project_path = "/tmp/project"

    with (
        patch("gobby.servers.websocket.chat.droid_backend.shutil.which", return_value="/bin/droid"),
        patch(
            "gobby.servers.websocket.chat.droid_backend.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        first = [event async for event in session.send_message("one")]
        second = [event async for event in session.send_message("two")]

    writes = [json.loads(write.decode("utf-8")) for write in process.stdin.writes]
    assert [write["method"] for write in writes] == [
        "droid.initialize_session",
        "droid.add_user_message",
        "droid.add_user_message",
    ]
    assert [event.content for event in first if isinstance(event, TextChunk)] == ["First"]
    assert [event.content for event in second if isinstance(event, TextChunk)] == ["Second"]
    assert process.terminated is False


@pytest.mark.asyncio
async def test_send_message_reattaches_dead_process_before_next_turn() -> None:
    processes = [
        _FakeProcess([_session_init_line()] + _turn_response_lines("First")),
        _FakeProcess([_session_init_line()] + _turn_response_lines("Second")),
    ]
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)
    session.project_path = "/tmp/project"

    with (
        patch("gobby.servers.websocket.chat.droid_backend.shutil.which", return_value="/bin/droid"),
        patch(
            "gobby.servers.websocket.chat.droid_backend.asyncio.create_subprocess_exec",
            side_effect=processes,
        ) as create_process,
    ):
        await backend.attach_session(session, model="gpt-5.4")
        first = [event async for event in session.send_message("one")]
        processes[0].returncode = 0
        second = [event async for event in session.send_message("two")]

    assert create_process.call_count == 2
    first_writes = [json.loads(write.decode("utf-8")) for write in processes[0].stdin.writes]
    second_writes = [json.loads(write.decode("utf-8")) for write in processes[1].stdin.writes]
    assert [write["method"] for write in first_writes] == [
        "droid.initialize_session",
        "droid.add_user_message",
    ]
    assert [write["method"] for write in second_writes] == [
        "droid.initialize_session",
        "droid.add_user_message",
    ]
    assert second_writes[0]["params"]["sessionId"] == "droid-session-1"
    assert [event.content for event in first if isinstance(event, TextChunk)] == ["First"]
    assert [event.content for event in second if isinstance(event, TextChunk)] == ["Second"]


@pytest.mark.asyncio
async def test_eof_before_result_yields_error_event() -> None:
    process = _FakeProcess(_fixture_lines("session_init.jsonl") + _fixture_lines("eof.jsonl"))
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)

    with (
        patch("gobby.servers.websocket.chat.droid_backend.shutil.which", return_value="/bin/droid"),
        patch(
            "gobby.servers.websocket.chat.droid_backend.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session)
        events = [event async for event in session.send_message("hello")]

    assert any(
        isinstance(event, TextChunk) and "Droid stream ended" in event.content for event in events
    )
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_switch_model_respawns_session_object_first() -> None:
    processes = [
        _FakeProcess(_fixture_lines("session_init.jsonl")),
        _FakeProcess([_session_init_line("gpt-5.4-mini")]),
    ]
    backend = DroidWebChatBackend()
    session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)

    with (
        patch("gobby.servers.websocket.chat.droid_backend.shutil.which", return_value="/bin/droid"),
        patch(
            "gobby.servers.websocket.chat.droid_backend.asyncio.create_subprocess_exec",
            side_effect=processes,
        ) as create_process,
    ):
        await backend.attach_session(session, model="gpt-5.4")
        await backend.switch_model(session, "gpt-5.4-mini")

    assert create_process.call_count == 2
    second_command = create_process.call_args_list[1].args
    model_index = second_command.index("--model")
    assert second_command[model_index + 1] == "gpt-5.4-mini"
    assert processes[0].terminated is True
    assert session._model == "gpt-5.4-mini"


def test_public_backend_methods_use_session_object_first_contract() -> None:
    for method_name in ("attach_session", "detach_session", "send_message", "switch_model"):
        params = list(inspect.signature(getattr(DroidWebChatBackend, method_name)).parameters)
        assert params[:2] == ["self", "session"]


@pytest.mark.asyncio
async def test_health_reports_missing_droid() -> None:
    backend = DroidWebChatBackend()

    with patch("gobby.servers.websocket.chat.droid_backend.shutil.which", return_value=None):
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
    assert session._connected is True
    assert "conv-droid" in backend._handles
    await backend.detach_session(session)
    assert session._connected is False
    assert backend._handles == {}
