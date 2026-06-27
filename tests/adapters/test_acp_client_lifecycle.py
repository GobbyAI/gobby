"""Tests for shared ACP client lifecycle behavior."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from gobby.adapters.acp_client import ACPClient
from gobby.adapters.acp_session_state import ACPSessionState

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

    state.update_agent_capabilities({"loadSession": True})
    capabilities = state.agent_capabilities
    capabilities["loadSession"] = False

    state.update_session_info(
        {
            "session": {"sessionId": "nested-session"},
            "roots": [{"uri": "file:///workspace"}, {"path": "/tmp/project"}],
        },
    )

    assert state.supports_session_load() is True
    assert state.agent_capabilities == {"loadSession": True}
    assert state.session_id == "nested-session"
    assert state.root_uris == ("file:///workspace", "/tmp/project")

    state.update_session_info({"sessionId": "fallback-session"}, fallback_roots=("/tmp/fallback",))

    assert state.root_uris == ("/tmp/fallback",)


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
