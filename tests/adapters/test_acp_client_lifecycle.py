"""Tests for shared ACP client lifecycle behavior."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from gobby.adapters.acp_client import ACPClient
from gobby.adapters.acp_commands import normalize_available_commands
from gobby.adapters.acp_session_state import ACPSessionState
from gobby.adapters.qwen_acp_client import QwenACPClient

pytestmark = pytest.mark.unit


class _StubACPClient(ACPClient):
    cli_name = "stub-acp"
    display_name = "Stub ACP"
    prompt_timeout_env = "GOBBY_STUB_ACP_PROMPT_TIMEOUT_SECONDS"


class _RecordingStdin:
    def __init__(self) -> None:
        self.buffer = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _LineStdout:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._lines = [(json.dumps(payload) + "\n").encode() for payload in payloads]

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _EmptyStderr:
    async def read(self, _n: int = -1) -> bytes:
        return b""


class _FakeProcess:
    pid = 123

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.stdin = _RecordingStdin()
        self.stdout = _LineStdout(payloads)
        self.stderr = _EmptyStderr()
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


def _written_messages(process: _FakeProcess) -> list[dict[str, Any]]:
    return [json.loads(line) for line in process.stdin.buffer.decode().splitlines() if line.strip()]


def test_session_state_copies_capabilities_and_tracks_roots() -> None:
    state = ACPSessionState()

    state.update_agent_capabilities({"loadSession": True, "sessionCapabilities": {"list": True}})
    capabilities = state.agent_capabilities
    capabilities["loadSession"] = False
    capabilities["sessionCapabilities"]["list"] = False

    state.update_session_info(
        {
            "session": {"sessionId": "nested-session"},
            "roots": [{"uri": "file:///workspace"}, {"path": "/tmp/project"}],
        },
    )

    assert state.supports_session_load() is True
    assert state.agent_capabilities == {
        "loadSession": True,
        "sessionCapabilities": {"list": True},
    }
    assert state.session_id == "nested-session"
    assert state.root_uris == ("file:///workspace", "/tmp/project")
    session_info = state.session_info
    session_info["roots"][0]["uri"] = "file:///mutated"
    assert state.session_info["roots"][0]["uri"] == "file:///workspace"

    state.update_session_info({"sessionId": "fallback-session"}, fallback_roots=("/tmp/fallback",))

    assert state.root_uris == ("/tmp/fallback",)


def test_normalize_available_commands_uses_current_acp_schema() -> None:
    assert normalize_available_commands(
        [
            {
                "name": "research",
                "description": "Research a topic",
                "input": {"hint": "topic"},
            },
            {"name": " summarize ", "description": " Summarize context "},
            {"name": "", "description": "missing name"},
            {"name": "broken", "description": ""},
            {"name": "ignored-input", "description": "No hint", "input": {"hint": ""}},
            "not-a-command",
        ]
    ) == [
        {
            "name": "research",
            "description": "Research a topic",
            "input": {"hint": "topic"},
        },
        {"name": "summarize", "description": "Summarize context"},
        {"name": "ignored-input", "description": "No hint"},
    ]


@pytest.mark.asyncio
async def test_start_advertises_terminal_capability_and_gates_session_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}},
            },
            {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "new-session"}},
        ]
    )

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    client = _StubACPClient(cli_path="/usr/bin/stub-acp")

    await client.start(session_id="old-session")

    messages = _written_messages(process)
    assert messages[0]["method"] == "initialize"
    assert messages[0]["params"]["clientCapabilities"] == {
        "terminal": True,
        "fs": {
            "readTextFile": True,
            "writeTextFile": True,
        },
    }
    assert client.agent_capabilities == {"loadSession": False}
    assert [message["method"] for message in messages] == ["initialize", "session/new"]
    assert client.session_id == "new-session"

    await client.stop()


@pytest.mark.asyncio
async def test_daemon_spawned_qwen_acp_disables_terminal_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"protocolVersion": 1, "agentCapabilities": {}},
            }
        ]
    )
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    client = QwenACPClient(cli_path="/usr/bin/qwen")

    await client.start(auto_session=False)

    assert captured["args"] == ("/usr/bin/qwen", "--acp")
    assert captured["env"]["GOBBY_HOOKS_DISABLED"] == "1"
    assert captured["env"]["GOBBY_ACP_CHILD"] == "1"

    await client.stop()


@pytest.mark.asyncio
async def test_start_logs_initialize_response_with_provider_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    init_result = {"protocolVersion": 1, "agentCapabilities": {}}
    process = _FakeProcess(
        [
            {"jsonrpc": "2.0", "id": 1, "result": init_result},
            {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "new-session"}},
        ]
    )

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    client = _StubACPClient(cli_path="/usr/bin/stub-acp", purpose="diagnostics")

    with caplog.at_level(logging.DEBUG, logger="gobby.adapters.acp_client"):
        await client.start()

    records = [
        record for record in caplog.records if record.getMessage() == "ACP initialize response"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.provider == "stub-acp"
    assert record.provider_display == "Stub ACP"
    assert record.purpose == "diagnostics"
    assert record.payload == init_result

    await client.stop()


def test_normalize_notification_maps_session_update_variants() -> None:
    plan = _StubACPClient._normalize_notification(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "plan",
                    "entries": [{"content": "Inspect", "status": "pending"}],
                },
            },
        }
    )
    mode = _StubACPClient._normalize_notification(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "current_mode_update",
                    "currentModeId": "yolo",
                },
            },
        }
    )
    session_info = _StubACPClient._normalize_notification(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "session_info_update",
                    "sessionInfo": {
                        "title": "ACP title",
                        "updatedAt": "2026-06-27T05:00:00Z",
                    },
                },
            },
        }
    )
    usage = _StubACPClient._normalize_notification(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "usage_update",
                    "size": 1000,
                    "used": 250,
                    "cost": {"currency": "USD", "amount": 0.01},
                },
            },
        }
    )
    available_commands = _StubACPClient._normalize_notification(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": [
                        {
                            "name": "research",
                            "description": "Research a topic",
                            "input": {"hint": "topic"},
                        }
                    ],
                },
            },
        }
    )
    rich_chunk = _StubACPClient._normalize_notification(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": [
                        {"type": "text", "text": "See "},
                        {
                            "type": "resource_link",
                            "uri": "file:///src/app.py",
                            "name": "src/app.py",
                        },
                    ],
                },
            },
        }
    )
    tool_call = _StubACPClient._normalize_notification(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tool-1",
                    "title": "Edit",
                    "kind": "edit",
                    "status": "pending",
                    "rawInput": {"path": "src/app.py"},
                },
            },
        }
    )
    tool_update = _StubACPClient._normalize_notification(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tool-1",
                    "status": "completed",
                    "content": [
                        {
                            "type": "diff",
                            "path": "src/app.py",
                            "oldText": "old",
                            "newText": "new",
                        }
                    ],
                    "rawOutput": {"stdout": "ok"},
                },
            },
        }
    )
    unknown = _StubACPClient._normalize_notification(
        {
            "method": "session/update",
            "params": {"update": {"sessionUpdate": "future_update", "value": True}},
        }
    )

    assert plan.event_type == "plan_update"
    assert plan.data["entries"][0]["content"] == "Inspect"
    assert mode.event_type == "current_mode_update"
    assert mode.data["current_mode_id"] == "yolo"
    assert session_info.event_type == "session_info_update"
    assert session_info.data["session_info"]["title"] == "ACP title"
    assert usage.event_type == "usage_update"
    assert usage.data["used"] == 250
    assert usage.data["cost"]["amount"] == 0.01
    assert available_commands.event_type == "available_commands_update"
    assert available_commands.data["commands"][0]["name"] == "research"
    assert rich_chunk.event_type == "content_delta"
    assert rich_chunk.data["content"] == "See "
    assert rich_chunk.data["content_blocks"] == [
        {
            "type": "resource_link",
            "uri": "file:///src/app.py",
            "name": "src/app.py",
        }
    ]
    assert tool_call.event_type == "tool_call"
    assert tool_call.data["tool_status"] == "pending"
    assert tool_call.data["tool_kind"] == "edit"
    assert tool_update.event_type == "tool_result"
    assert tool_update.data["success"] is True
    assert tool_update.data["raw_output"] == {"stdout": "ok"}
    assert tool_update.data["content_blocks"][0]["type"] == "diff"
    assert unknown.event_type == "future_update"


@pytest.mark.asyncio
async def test_send_accepts_structured_prompt_blocks() -> None:
    process = _FakeProcess([{"jsonrpc": "2.0", "id": 1, "result": {"stats": {}}}])
    client = _StubACPClient(cli_path="/usr/bin/stub-acp")
    client._process = process
    client._started = True
    client._session_state.update_session_info({"sessionId": "sess-1"})

    prompt = [
        {"type": "text", "text": "hello"},
        {"type": "resource_link", "uri": "file:///src/app.py", "name": "src/app.py"},
    ]

    events = [event async for event in client.send(prompt)]

    assert events[-1].event_type == "result"
    assert _written_messages(process)[0]["params"]["prompt"] == prompt


@pytest.mark.asyncio
async def test_start_rejects_incompatible_protocol_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"protocolVersion": 2, "agentCapabilities": {}},
            }
        ]
    )

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    client = _StubACPClient(cli_path="/usr/bin/stub-acp")

    with pytest.raises(RuntimeError, match="protocol version mismatch"):
        await client.start(auto_session=False)


@pytest.mark.asyncio
async def test_session_load_result_null_preserves_requested_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
            },
            {"jsonrpc": "2.0", "id": 2, "result": None},
        ]
    )

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    client = _StubACPClient(cli_path="/usr/bin/stub-acp")

    await client.start(session_id="requested-session")

    messages = _written_messages(process)
    assert messages[1]["method"] == "session/load"
    assert client.session_id == "requested-session"
    assert client.session_info == {}

    await client.stop()


@pytest.mark.asyncio
async def test_cancel_session_sends_out_of_band_notification() -> None:
    process = _FakeProcess([])
    client = _StubACPClient(cli_path="/usr/bin/stub-acp")
    client._process = process
    client._started = True
    client._session_state.update_session_info({"sessionId": "sess-1"})

    await client.cancel_session()

    assert _written_messages(process) == [
        {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": "sess-1"},
        }
    ]


@pytest.mark.asyncio
async def test_send_cancellation_preserves_cancelled_error_when_cancel_session_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _StubACPClient(cli_path="/usr/bin/stub-acp")
    client._started = True
    client._session_state.update_session_info({"sessionId": "sess-1"})
    client._process = _FakeProcess([])
    cancelled_sessions: list[str | None] = []

    async def write_json_rpc_message(_message: dict[str, Any]) -> None:
        return None

    async def cancel_session(session_id: str | None = None) -> None:
        cancelled_sessions.append(session_id)
        raise BrokenPipeError("closed")

    class CancelledStream:
        def __aiter__(self) -> CancelledStream:
            return self

        async def __anext__(self) -> Any:
            raise asyncio.CancelledError

    def read_stream(**_kwargs: Any) -> CancelledStream:
        return CancelledStream()

    monkeypatch.setattr(client, "_write_json_rpc_message", write_json_rpc_message)
    monkeypatch.setattr(client, "cancel_session", cancel_session)
    monkeypatch.setattr(client, "_read_stream", read_stream)

    with caplog.at_level(logging.DEBUG, logger="gobby.adapters.acp_client"):
        with pytest.raises(asyncio.CancelledError):
            async for _event in client.send("hello"):
                pass

    assert cancelled_sessions == ["sess-1"]
    assert "session/cancel failed during prompt cancellation" in caplog.text
