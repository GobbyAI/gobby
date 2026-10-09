"""Golden-fixture ACP contract tests for ACP subprocess streams."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.adapters.grok_acp_client import GrokACPClient
from gobby.utils.child_supervisor import supervised_argv

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "acp_contract"
PROMPT_TEXT = "contract ping"


class FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [(line if line.endswith("\n") else f"{line}\n").encode() for line in lines]
        self._index = 0

    async def readline(self) -> bytes:
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line


class FakeStderr:
    async def read(self, n: int = -1) -> bytes:
        del n
        return b""


class FakeACPProcess:
    def __init__(self, stdout_lines: list[str]) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(stdout_lines)
        self.stderr = FakeStderr()

    async def wait(self) -> int:
        return self.returncode or 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURE_DIR / name).read_text().splitlines()


def _written_requests(process: FakeACPProcess) -> list[dict[str, Any]]:
    return [json.loads(write.decode()) for write in process.stdin.writes]


def _assert_initialize_request(request: dict[str, Any]) -> None:
    assert request["method"] == "initialize"
    assert request["jsonrpc"] == "2.0"
    assert request["params"]["protocolVersion"] == 1
    assert request["params"]["clientInfo"] == {"name": "gobby", "version": "1.0.0"}
    assert request["params"]["clientCapabilities"] == {
        "terminal": True,
        "fs": {
            "readTextFile": True,
            "writeTextFile": True,
        },
    }


def _assert_authenticate_request(request: dict[str, Any]) -> None:
    assert request["method"] == "authenticate"
    assert request["jsonrpc"] == "2.0"
    assert request["params"] == {"methodId": "cached_token"}


def _assert_session_request(
    request: dict[str, Any],
    *,
    method: str,
    session_id: str | None,
) -> None:
    assert request["method"] == method
    assert request["jsonrpc"] == "2.0"
    assert request["params"]["cwd"] == "."
    assert request["params"]["mcpServers"] == []
    if session_id is None:
        assert "sessionId" not in request["params"]
    else:
        assert request["params"]["sessionId"] == session_id


def _assert_prompt_request(request: dict[str, Any], *, session_id: str) -> None:
    assert request["method"] == "session/prompt"
    assert request["jsonrpc"] == "2.0"
    assert request["params"]["sessionId"] == session_id
    assert request["params"]["prompt"] == [{"type": "text", "text": PROMPT_TEXT}]


async def test_grok_recorded_fixture_stream_drives_authenticated_client_flow() -> None:
    process = FakeACPProcess(_fixture_lines("grok-0.1.216-session-new-prompt.stdout.jsonl"))

    with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/grok"):
        with patch(
            "gobby.utils.spawn.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ) as create_process:
            client = GrokACPClient()
            await client.start()
            events = [event async for event in client.send(PROMPT_TEXT)]

    assert create_process.call_args.args == tuple(
        supervised_argv(["/usr/bin/grok", "agent", "--no-leader", "--always-approve", "stdio"])
    )
    requests = _written_requests(process)
    assert [request.get("method") for request in requests] == [
        "initialize",
        "authenticate",
        "session/new",
        "session/prompt",
    ]
    _assert_initialize_request(requests[0])
    _assert_authenticate_request(requests[1])
    _assert_session_request(requests[2], method="session/new", session_id=None)
    _assert_prompt_request(requests[3], session_id="grok-new-session")

    assert any(event.event_type == "thinking_delta" for event in events)
    assert any(event.event_type == "content_delta" for event in events)
    assert events[-1].event_type == "result"


async def test_grok_load_fixture_handles_terminal_client_request() -> None:
    process = FakeACPProcess(_fixture_lines("grok-0.1.216-session-load-tool-prompt.stdout.jsonl"))

    class FakeTerminalManager:
        async def create(
            self, params: dict[str, Any], *, default_cwd: str | None = None
        ) -> dict[str, str]:
            assert params["command"] == "/bin/bash"
            assert params["args"] == ["-lc", "pwd"]
            assert params["cwd"] == "/tmp"
            assert default_cwd is None
            return {"terminalId": "term-fixture"}

    with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/grok"):
        with patch(
            "gobby.utils.spawn.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ):
            client = GrokACPClient()
            client._terminal_manager = FakeTerminalManager()
            await client.start(session_id="grok-existing-session")
            events = [event async for event in client.send(PROMPT_TEXT)]

    requests = _written_requests(process)
    request_methods = [request.get("method") for request in requests if request.get("method")]
    assert request_methods == [
        "initialize",
        "authenticate",
        "session/load",
        "session/prompt",
    ]
    assert any(event.event_type == "tool_call" for event in events)
    assert any(event.event_type == "tool_result" for event in events)
    responses = [request for request in requests if request.get("id") == 0 and "result" in request]
    assert responses
    assert responses[0]["result"] == {"terminalId": "term-fixture"}
