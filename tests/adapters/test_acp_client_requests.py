"""Tests for client-directed ACP JSON-RPC request handling."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.adapters import acp_client_requests, acp_terminal
from gobby.adapters.acp_terminal import ACPTerminalManager
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

    class FakeStdout:
        async def read(self, _n: int) -> bytes:
            return b""

    class FakeProcess:
        returncode = 0
        stdout = FakeStdout()

        async def wait(self) -> int:
            return 0

        def kill(self) -> None:
            return None

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv(UV_CACHE_DIR, "/tmp/gobby-uv-cache")
    monkeypatch.setenv("SECRET_TOKEN", "should-not-leak")
    monkeypatch.setattr(
        acp_terminal.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    manager = ACPTerminalManager()
    result = await manager.create(
        {
            "command": "printf",
            "args": ["ok"],
            "cwd": str(tmp_path),
            "env": [{"name": "NODE_ENV", "value": "test"}],
        }
    )

    assert result["terminalId"].startswith("term_")
    assert captured["args"][:2] == ("printf", "ok")
    assert captured["cwd"] == str(tmp_path)
    env = captured["env"]
    assert env["PATH"] == "/usr/bin"
    assert env[UV_CACHE_DIR] == "/tmp/gobby-uv-cache"
    assert env["NODE_ENV"] == "test"
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


def _recording_client(root: Path | None = None) -> SimpleNamespace:
    root_uris = (root.as_uri(),) if root is not None else ()
    return SimpleNamespace(
        _process=SimpleNamespace(stdin=_RecordingStdin()),
        _terminal_manager=ACPTerminalManager(),
        _cwd=None,
        _session_state=SimpleNamespace(root_uris=root_uris),
        session_id="sess-1",
        cli_name="gemini",
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
async def test_filesystem_read_uses_one_based_line_and_limit(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    client = _recording_client(tmp_path)
    request = {
        "jsonrpc": "2.0",
        "id": "read-1",
        "method": "fs/read_text_file",
        "params": {
            "sessionId": "sess-1",
            "path": str(target),
            "line": 2,
            "limit": 2,
        },
    }

    events = [event async for event in acp_client_requests.handle_client_request(client, request)]

    assert events == []
    response = _written_messages(client)[0]
    assert response["id"] == "read-1"
    assert response["result"] == {"content": "two\nthree\n"}


@pytest.mark.asyncio
async def test_filesystem_read_rejects_out_of_root_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    client = _recording_client(root)
    request = {
        "jsonrpc": "2.0",
        "id": "read-outside",
        "method": "fs/read_text_file",
        "params": {"sessionId": "sess-1", "path": str(outside)},
    }

    events = [event async for event in acp_client_requests.handle_client_request(client, request)]

    assert events == []
    response = _written_messages(client)[0]
    assert response["id"] == "read-outside"
    assert response["error"] == {"code": -32602, "message": "path is outside the ACP session root"}


@pytest.mark.asyncio
async def test_filesystem_write_creates_file_and_returns_null_result(tmp_path: Path) -> None:
    target = tmp_path / "created.txt"
    client = _recording_client(tmp_path)
    request = {
        "jsonrpc": "2.0",
        "id": "write-1",
        "method": "fs/write_text_file",
        "params": {
            "sessionId": "sess-1",
            "path": str(target),
            "content": "created\n",
        },
    }

    events = [event async for event in acp_client_requests.handle_client_request(client, request)]

    assert [event.event_type for event in events] == ["tool_call", "tool_result"]
    assert events[0].data["tool_name"] == "write_text_file"
    assert events[0].data["tool_input"] == {"path": str(target), "content": "created\n"}
    assert events[1].data["success"] is True
    assert events[1].data["result"] == {"path": str(target), "bytes": 8}
    assert target.read_text(encoding="utf-8") == "created\n"
    response = _written_messages(client)[0]
    assert response["id"] == "write-1"
    assert response["result"] is None


@pytest.mark.asyncio
async def test_filesystem_write_replaces_existing_file_atomically(tmp_path: Path) -> None:
    target = tmp_path / "config.txt"
    target.write_text("old\n", encoding="utf-8")
    client = _recording_client(tmp_path)
    request = {
        "jsonrpc": "2.0",
        "id": "write-2",
        "method": "fs/write_text_file",
        "params": {
            "sessionId": "sess-1",
            "path": str(target),
            "content": "new\n",
        },
    }

    events = [event async for event in acp_client_requests.handle_client_request(client, request)]

    assert [event.event_type for event in events] == ["tool_call", "tool_result"]
    assert target.read_text(encoding="utf-8") == "new\n"
    assert _written_messages(client)[0]["result"] is None


@pytest.mark.asyncio
async def test_filesystem_write_does_not_execute_when_pre_tool_blocks(tmp_path: Path) -> None:
    target = tmp_path / "blocked.txt"
    client = _recording_client(tmp_path)
    pre_tool_callback = AsyncMock(return_value={"decision": "deny", "reason": "no writes"})
    request = {
        "jsonrpc": "2.0",
        "id": "write-blocked",
        "method": "fs/write_text_file",
        "params": {
            "sessionId": "sess-1",
            "path": str(target),
            "content": "blocked\n",
        },
    }

    events = [
        event
        async for event in acp_client_requests.handle_client_request(
            client,
            request,
            pre_tool_callback=pre_tool_callback,
        )
    ]

    assert events == []
    pre_tool_callback.assert_awaited_once_with(
        {
            "tool_name": "write_text_file",
            "tool_input": {"path": str(target), "content": "blocked\n"},
        }
    )
    assert not target.exists()
    response = _written_messages(client)[0]
    assert response["id"] == "write-blocked"
    assert response["error"] == {"code": -32000, "message": "no writes"}


@pytest.mark.asyncio
async def test_terminal_request_lifecycle_uses_spec_methods(tmp_path: Path) -> None:
    """Terminal methods create a handle, read output, wait, kill, and release by ID."""
    client = _recording_client()
    create_request = {
        "jsonrpc": "2.0",
        "id": "create-1",
        "method": "terminal/create",
        "params": {
            "sessionId": "sess-1",
            "command": sys.executable,
            "args": [
                "-c",
                "import sys; sys.stdout.write('hello'); sys.stdout.flush(); sys.stderr.write('!')",
            ],
            "cwd": str(tmp_path),
            "outputByteLimit": 1024,
        },
    }

    events = [
        event async for event in acp_client_requests.handle_client_request(client, create_request)
    ]

    assert [event.event_type for event in events] == ["tool_call", "tool_result"]
    terminal_id = _written_messages(client)[0]["result"]["terminalId"]
    assert terminal_id.startswith("term_")

    for request_id, method in [
        ("wait-1", "terminal/wait_for_exit"),
        ("output-1", "terminal/output"),
        ("kill-1", "terminal/kill"),
        ("release-1", "terminal/release"),
    ]:
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {"sessionId": "sess-1", "terminalId": terminal_id},
        }
        assert [
            event async for event in acp_client_requests.handle_client_request(client, request)
        ] == []

    messages = _written_messages(client)
    assert messages[1]["result"] == {"exitCode": 0, "signal": None}
    assert messages[2]["result"] == {
        "output": "hello!",
        "truncated": False,
        "exitStatus": {"exitCode": 0, "signal": None},
    }
    assert messages[3]["result"] == {}
    assert messages[4]["result"] == {}


@pytest.mark.asyncio
async def test_terminal_output_rejects_invalid_terminal_id() -> None:
    """Unknown or released terminal IDs return JSON-RPC invalid-params errors."""
    client = _recording_client()

    events = [
        event
        async for event in acp_client_requests.handle_client_request(
            client,
            {
                "jsonrpc": "2.0",
                "id": "output-1",
                "method": "terminal/output",
                "params": {"sessionId": "sess-1", "terminalId": "term_missing"},
            },
        )
    ]

    assert events == []
    response = _written_messages(client)[0]
    assert response["id"] == "output-1"
    assert response["error"]["code"] == -32602
    assert "term_missing" in response["error"]["message"]


@pytest.mark.asyncio
async def test_terminal_wait_for_exit_times_out_with_json_rpc_error() -> None:
    """A hanging wait_for_exit returns a bounded error instead of blocking the reader."""
    client = _recording_client()
    client._request_timeout = 0.001

    class HangingTerminalManager:
        async def wait_for_exit(self, _terminal_id: str) -> dict[str, int | str | None]:
            await asyncio.Event().wait()
            return {"exitCode": 0, "signal": None}

    client._terminal_manager = HangingTerminalManager()

    events = [
        event
        async for event in acp_client_requests.handle_client_request(
            client,
            {
                "jsonrpc": "2.0",
                "id": "wait-1",
                "method": "terminal/wait_for_exit",
                "params": {"sessionId": "sess-1", "terminalId": "term_hangs"},
            },
        )
    ]

    assert events == []
    response = _written_messages(client)[0]
    assert response["id"] == "wait-1"
    assert response["error"]["code"] == -32000
    assert "terminal/wait_for_exit timed out" in response["error"]["message"]


@pytest.mark.asyncio
async def test_terminal_output_truncates_from_end_on_character_boundary(tmp_path: Path) -> None:
    """Output retention keeps the tail and never returns broken UTF-8."""
    manager = ACPTerminalManager()
    result = await manager.create(
        {
            "command": sys.executable,
            "args": ["-c", "print('alphaéomega')"],
            "cwd": str(tmp_path),
            "outputByteLimit": 7,
        }
    )
    terminal_id = result["terminalId"]

    assert await manager.wait_for_exit(terminal_id) == {"exitCode": 0, "signal": None}
    output = await manager.output(terminal_id)
    await manager.release(terminal_id)

    assert output["truncated"] is True
    assert output["output"] == "omega\n"


@pytest.mark.asyncio
async def test_terminal_create_rejects_relative_cwd() -> None:
    """ACP terminal/create only accepts absolute cwd values."""
    client = _recording_client()

    events = [
        event
        async for event in acp_client_requests.handle_client_request(
            client,
            {
                "jsonrpc": "2.0",
                "id": "create-relative",
                "method": "terminal/create",
                "params": {
                    "sessionId": "sess-1",
                    "command": sys.executable,
                    "args": ["-c", "print('nope')"],
                    "cwd": "relative/path",
                },
            },
        )
    ]

    assert events == []
    response = _written_messages(client)[0]
    assert response["id"] == "create-relative"
    assert response["error"]["code"] == -32602
    assert "cwd must be an absolute path" in response["error"]["message"]


@pytest.mark.asyncio
async def test_terminal_create_does_not_execute_when_pre_tool_blocks(
    tmp_path: Path,
) -> None:
    """A Gobby pre-tool block returns a JSON-RPC error without execution."""
    client = _recording_client()
    pre_tool_callback = AsyncMock(return_value={"decision": "deny", "reason": "no shell"})
    request = {
        "jsonrpc": "2.0",
        "id": "terminal-1",
        "method": "terminal/create",
        "params": {
            "command": sys.executable,
            "args": ["-c", "print('nope')"],
            "cwd": str(tmp_path),
            "outputByteLimit": 4096,
        },
    }

    events = [
        event
        async for event in acp_client_requests.handle_client_request(
            client,
            request,
            pre_tool_callback=pre_tool_callback,
        )
    ]

    assert events == []
    pre_tool_callback.assert_awaited_once_with(
        {
            "tool_name": "run_terminal_command",
            "tool_input": {
                "command": sys.executable,
                "args": ["-c", "print('nope')"],
                "cwd": str(tmp_path),
                "env": [],
                "outputByteLimit": 4096,
            },
        }
    )
    response = _written_messages(client)[0]
    assert response["id"] == "terminal-1"
    assert response["error"] == {"code": -32000, "message": "no shell"}


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
