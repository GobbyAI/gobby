"""Tests for client-directed ACP JSON-RPC request handling."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.adapters import acp_client_requests
from gobby.agents.constants import UV_CACHE_DIR

pytestmark = pytest.mark.unit


class _ClosedStdin:
    def write(self, _data: bytes) -> None:
        raise BrokenPipeError


@pytest.mark.asyncio
async def test_write_json_rpc_result_ignores_closed_client_pipe() -> None:
    """A closed ACP client pipe is ignored when sending a JSON-RPC result."""
    client = SimpleNamespace(_process=SimpleNamespace(stdin=_ClosedStdin()))

    result = await acp_client_requests.write_json_rpc_result(client, "request-1", {"ok": True})

    assert result is None


@pytest.mark.asyncio
async def test_terminal_create_env_uses_minimal_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Terminal requests preserve only the minimal env allowlist plus ACP guard vars."""
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"ok", b""

    async def fake_create_subprocess_shell(*_args: Any, **kwargs: Any) -> FakeProcess:
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv(UV_CACHE_DIR, "/tmp/gobby-uv-cache")
    monkeypatch.setenv("SECRET_TOKEN", "should-not-leak")
    monkeypatch.setattr(
        acp_client_requests.asyncio,
        "create_subprocess_shell",
        fake_create_subprocess_shell,
    )

    result = await acp_client_requests._run_terminal_create(
        "printf ok",
        cwd=str(tmp_path),
        timeout_seconds=1.0,
        output_limit=1024,
    )

    assert result["exitCode"] == 0
    env = captured["env"]
    assert env["PATH"] == "/usr/bin"
    assert env[UV_CACHE_DIR] == "/tmp/gobby-uv-cache"
    assert env["GOBBY_HOOKS_DISABLED"] == "1"
    assert env["GOBBY_ACP_CHILD_TOOL"] == "1"
    assert "SECRET_TOKEN" not in env


class _RecordingStdin:
    def __init__(self) -> None:
        self.buffer = b""

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None


def _recording_client() -> SimpleNamespace:
    return SimpleNamespace(
        _process=SimpleNamespace(stdin=_RecordingStdin()),
        display_name="Gemini",
    )


def _written_messages(client: SimpleNamespace) -> list[dict[str, Any]]:
    raw: bytes = client._process.stdin.buffer
    return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]


# Mirrors a real Gemini ACP `session/request_permission` option set.
_REAL_PERMISSION_OPTIONS = [
    {"optionId": "proceed_always_server", "name": "Allow all", "kind": "allow_always"},
    {"optionId": "proceed_always_tool", "name": "Allow tool", "kind": "allow_always"},
    {"optionId": "proceed_once", "name": "Allow", "kind": "allow_once"},
    {"optionId": "cancel", "name": "Reject", "kind": "reject_once"},
]


def _permission_request(options: list[Any], request_id: int = 0) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "sess-1",
            "options": options,
            "toolCall": {"toolCallId": "tc-1", "title": "list_mcp_servers"},
        },
    }


def test_select_permission_option_prefers_allow_once() -> None:
    """allow_once is preferred over allow_always so Gobby is reconsulted per call."""
    option_id, kind = acp_client_requests._select_permission_option(_REAL_PERMISSION_OPTIONS)

    assert option_id == "proceed_once"
    assert kind == "allow_once"


def test_select_permission_option_falls_back_to_allow_always() -> None:
    """When no allow_once is offered, the first allow_always option is selected."""
    option_id, kind = acp_client_requests._select_permission_option(
        [
            {"optionId": "proceed_always_server", "kind": "allow_always"},
            {"optionId": "cancel", "kind": "reject_once"},
        ]
    )

    assert option_id == "proceed_always_server"
    assert kind == "allow_always"


def test_select_permission_option_returns_none_without_allow() -> None:
    """Reject-only / malformed option sets yield no allow selection."""
    assert acp_client_requests._select_permission_option(
        [{"optionId": "cancel", "kind": "reject_once"}]
    ) == (None, None)
    assert acp_client_requests._select_permission_option(None) == (None, None)
    assert acp_client_requests._select_permission_option(["bad", {"kind": "allow_once"}]) == (
        None,
        None,
    )


@pytest.mark.asyncio
async def test_request_permission_auto_approves_allow_once() -> None:
    """A permission request is answered with a well-formed selected outcome."""
    client = _recording_client()

    events = [
        event
        async for event in acp_client_requests.handle_client_request(
            client, _permission_request(_REAL_PERMISSION_OPTIONS)
        )
    ]

    assert events == []
    messages = _written_messages(client)
    assert len(messages) == 1
    response = messages[0]
    assert response["id"] == 0
    assert "error" not in response
    assert response["result"] == {"outcome": {"outcome": "selected", "optionId": "proceed_once"}}


@pytest.mark.asyncio
async def test_request_permission_cancels_when_no_allow_option() -> None:
    """No allow option declines gracefully instead of erroring (no '[object Object]')."""
    client = _recording_client()

    await acp_client_requests._handle_request_permission_request(
        client, _permission_request([{"optionId": "cancel", "kind": "reject_once"}])
    )

    messages = _written_messages(client)
    assert len(messages) == 1
    response = messages[0]
    assert "error" not in response
    assert response["result"] == {"outcome": {"outcome": "cancelled"}}


@pytest.mark.asyncio
async def test_request_permission_cancels_when_pre_tool_blocks() -> None:
    """A Gobby pre-tool block declines the ACP permission grant."""
    client = _recording_client()
    pre_tool_callback = AsyncMock(return_value={"decision": "block", "reason": "policy denied"})

    await acp_client_requests._handle_request_permission_request(
        client,
        _permission_request(_REAL_PERMISSION_OPTIONS),
        pre_tool_callback=pre_tool_callback,
    )

    pre_tool_callback.assert_awaited_once_with(
        {
            "tool_name": "list_mcp_servers",
            "tool_input": {"toolCallId": "tc-1", "title": "list_mcp_servers"},
        }
    )
    response = _written_messages(client)[0]
    assert "error" not in response
    assert response["result"] == {"outcome": {"outcome": "cancelled"}}


@pytest.mark.asyncio
async def test_terminal_create_does_not_execute_when_pre_tool_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Gobby pre-tool block returns a cancelled terminal result without execution."""
    client = _recording_client()
    pre_tool_callback = AsyncMock(return_value={"decision": "deny", "reason": "no shell"})
    run_terminal = AsyncMock()
    monkeypatch.setattr(acp_client_requests, "_run_terminal_create", run_terminal)
    request = {
        "jsonrpc": "2.0",
        "id": "terminal-1",
        "method": "terminal/create",
        "params": {
            "command": "printf nope",
            "cwd": str(tmp_path),
            "timeout": 1,
            "outputByteLimit": 4096,
        },
    }

    events = [
        event
        async for event in acp_client_requests._handle_terminal_create_request(
            client,
            request,
            pre_tool_callback=pre_tool_callback,
        )
    ]

    assert events == []
    run_terminal.assert_not_awaited()
    pre_tool_callback.assert_awaited_once_with(
        {
            "tool_name": "run_terminal_command",
            "tool_input": {
                "command": "printf nope",
                "cwd": str(tmp_path),
                "timeout": 1.0,
                "outputByteLimit": 4096,
            },
        }
    )
    response = _written_messages(client)[0]
    assert response["id"] == "terminal-1"
    assert response["result"] == {
        "exitCode": 1,
        "stdout": "",
        "stderr": "no shell",
        "error": "no shell",
        "timedOut": False,
        "truncated": False,
        "cancelled": True,
    }


@pytest.mark.asyncio
async def test_unknown_client_request_still_errors() -> None:
    """Genuinely unknown client methods keep returning a JSON-RPC method error."""
    client = _recording_client()

    events = [
        event
        async for event in acp_client_requests.handle_client_request(
            client, {"jsonrpc": "2.0", "id": 7, "method": "fs/unsupported", "params": {}}
        )
    ]

    assert events == []
    response = _written_messages(client)[0]
    assert response["id"] == 7
    assert response["error"]["code"] == -32601
