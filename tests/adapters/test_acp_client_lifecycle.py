"""Tests for shared ACP client lifecycle behavior."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gobby.adapters.acp_client import ACPClient

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
    assert messages[0]["params"]["clientCapabilities"] == {"terminal": True}
    assert client.agent_capabilities == {"loadSession": False}
    assert [message["method"] for message in messages] == ["initialize", "session/new"]
    assert client.session_id == "new-session"

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
    client._session_id = "sess-1"

    await client.cancel_session()

    assert _written_messages(process) == [
        {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": "sess-1"},
        }
    ]
