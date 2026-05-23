"""Golden-fixture ACP contract tests for Gemini and Qwen subprocess streams."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.adapters.acp_client import ACPClient, StreamEvent
from gobby.adapters.gemini_acp_client import GeminiACPClient
from gobby.adapters.grok_acp_client import GrokACPClient
from gobby.adapters.qwen_acp_client import QwenACPClient

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "acp_contract"
PROMPT_TEXT = "contract ping"


@dataclass(frozen=True)
class ACPFixtureCase:
    client_class: type[ACPClient]
    cli_name: str
    version: str
    new_fixture: str
    load_fixture: str
    new_session_id: str
    load_session_id: str


ACP_FIXTURE_CASES = [
    pytest.param(
        ACPFixtureCase(
            client_class=GeminiACPClient,
            cli_name="gemini",
            version="0.40.1",
            new_fixture="gemini-0.40.1-session-new-prompt.stdout.jsonl",
            load_fixture="gemini-0.40.1-session-load-prompt.stdout.jsonl",
            new_session_id="gemini-new-session",
            load_session_id="gemini-existing-session",
        ),
        id="gemini-0.40.1",
    ),
    pytest.param(
        ACPFixtureCase(
            client_class=QwenACPClient,
            cli_name="qwen",
            version="0.15.6",
            new_fixture="qwen-0.15.6-session-new-prompt.stdout.jsonl",
            load_fixture="qwen-0.15.6-session-load-prompt.stdout.jsonl",
            new_session_id="qwen-new-session",
            load_session_id="qwen-existing-session",
        ),
        id="qwen-0.15.6",
    ),
]


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
    async def read(self) -> bytes:
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


def _fixture_payloads(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in _fixture_lines(name) if line.strip()]


def _notification_payloads(name: str) -> list[dict[str, Any]]:
    return [payload for payload in _fixture_payloads(name) if "method" in payload]


def _written_requests(process: FakeACPProcess) -> list[dict[str, Any]]:
    return [json.loads(write.decode()) for write in process.stdin.writes]


def _assert_initialize_request(request: dict[str, Any]) -> None:
    assert request["method"] == "initialize"
    assert request["jsonrpc"] == "2.0"
    assert request["params"]["protocolVersion"] == 1
    assert request["params"]["clientInfo"] == {"name": "gobby", "version": "1.0.0"}
    assert request["params"]["clientCapabilities"] == {}


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


async def _drive_fixture(
    case: ACPFixtureCase,
    fixture_name: str,
    *,
    session_id: str | None,
) -> tuple[FakeACPProcess, list[StreamEvent], AsyncMock]:
    assert fixture_name.startswith(f"{case.cli_name}-{case.version}-")
    process = FakeACPProcess(_fixture_lines(fixture_name))

    with patch("gobby.adapters.acp_client.shutil.which", return_value=f"/usr/bin/{case.cli_name}"):
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ) as create_process:
            client = case.client_class()
            await client.start(session_id=session_id)
            events = [event async for event in client.send(PROMPT_TEXT)]

    return process, events, create_process


@pytest.mark.parametrize("case", ACP_FIXTURE_CASES)
@pytest.mark.parametrize(
    ("fixture_attr", "session_method"),
    [
        ("new_fixture", "session/new"),
        ("load_fixture", "session/load"),
    ],
)
async def test_recorded_acp_fixture_stream_drives_client_flow(
    case: ACPFixtureCase,
    fixture_attr: str,
    session_method: str,
) -> None:
    fixture_name = getattr(case, fixture_attr)
    is_load = session_method == "session/load"
    expected_session_id = case.load_session_id if is_load else case.new_session_id
    requested_session_id = expected_session_id if is_load else None

    process, events, create_process = await _drive_fixture(
        case,
        fixture_name,
        session_id=requested_session_id,
    )

    assert create_process.call_args.args[:2] == (f"/usr/bin/{case.cli_name}", "--acp")
    requests = _written_requests(process)
    assert [request["method"] for request in requests] == [
        "initialize",
        session_method,
        "session/prompt",
    ]
    _assert_initialize_request(requests[0])
    _assert_session_request(
        requests[1],
        method=session_method,
        session_id=requested_session_id,
    )
    _assert_prompt_request(requests[2], session_id=expected_session_id)

    assert any(event.event_type == "thinking_delta" for event in events)
    assert any(event.event_type == "content_delta" for event in events)
    assert events[-1].event_type == "result"


@pytest.mark.parametrize("case", ACP_FIXTURE_CASES)
def test_normalize_notification_handles_recorded_provider_payloads(
    case: ACPFixtureCase,
) -> None:
    notifications = [
        *_notification_payloads(case.new_fixture),
        *_notification_payloads(case.load_fixture),
    ]

    normalized = [case.client_class._normalize_notification(payload) for payload in notifications]
    content_deltas = [event for event in normalized if event.event_type == "content_delta"]
    thinking_deltas = [event for event in normalized if event.event_type == "thinking_delta"]

    assert len(content_deltas) == 2
    assert len(thinking_deltas) == 2
    assert all(event.data["content"] for event in content_deltas)
    assert all(event.data["content"] for event in thinking_deltas)


async def test_grok_recorded_fixture_stream_drives_authenticated_client_flow() -> None:
    process = FakeACPProcess(_fixture_lines("grok-0.1.216-session-new-prompt.stdout.jsonl"))

    with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/grok"):
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ) as create_process:
            client = GrokACPClient()
            await client.start()
            events = [event async for event in client.send(PROMPT_TEXT)]

    assert create_process.call_args.args[:5] == (
        "/usr/bin/grok",
        "agent",
        "--no-leader",
        "--always-approve",
        "stdio",
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

    with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/grok"):
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ):
            client = GrokACPClient()
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
    assert responses[0]["result"]["exitCode"] == 1
