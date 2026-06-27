"""Tests for ACP client session lifecycle methods (list/resume/close/delete).

Each method drives the shared ``ACPClient`` request/response path on a fake
subprocess harness, asserting the exact JSON-RPC method names and params, the
``additionalDirectories`` omitted-when-empty / present-when-supported behavior,
and ``UnsupportedACPMethodError`` when the matching capability is ungated.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.adapters.acp_client import ACPClient, UnsupportedACPMethodError
from gobby.servers.websocket.chat.backends.acp import ACPWebChatBackend

pytestmark = pytest.mark.unit


class _StubACPClient(ACPClient):
    cli_name = "stub-acp"
    display_name = "Stub ACP"
    prompt_timeout_env = "GOBBY_STUB_ACP_PROMPT_TIMEOUT_SECONDS"


class _StubACPBackend(ACPWebChatBackend):
    provider = "stub"
    display_name = "Stub"
    acp_client_cls = _StubACPClient


class _RecordingStdin:
    def __init__(self) -> None:
        self.buffer = b""

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


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
    pid = 321

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


def _last_request(process: _FakeProcess) -> dict[str, Any]:
    return _written_messages(process)[-1]


async def _start_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_capabilities: dict[str, Any],
    response_payloads: list[dict[str, Any]] | None = None,
) -> tuple[_StubACPClient, _FakeProcess]:
    """Start a stub client whose initialize advertises ``agent_capabilities``.

    ``auto_session=False`` keeps request ids deterministic: ``initialize`` is id
    ``1``, so the first lifecycle call is id ``2``.
    """
    process = _FakeProcess(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"protocolVersion": 1, "agentCapabilities": agent_capabilities},
            },
            *(response_payloads or []),
        ]
    )

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    client = _StubACPClient(cli_path="/usr/bin/stub-acp")
    await client.start(auto_session=False)
    return client, process


def _result(session_id: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": session_id}}


# --------------------------------------------------------------------------- #
# session/list
# --------------------------------------------------------------------------- #


async def test_list_sessions_sends_exact_method_and_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities={"sessionCapabilities": {"list": {}}},
        response_payloads=[
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"sessions": [{"sessionId": "s1"}], "nextCursor": "c1"},
            }
        ],
    )

    result = await client.list_sessions(cwd="/repo", cursor="c0")

    request = _last_request(process)
    assert request["method"] == "session/list"
    assert request["jsonrpc"] == "2.0"
    assert request["params"] == {"cwd": "/repo", "cursor": "c0"}
    assert result == {"sessions": [{"sessionId": "s1"}], "nextCursor": "c1"}

    await client.stop()


async def test_list_sessions_omits_optional_params_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities={"sessionCapabilities": {"list": {}}},
        response_payloads=[{"jsonrpc": "2.0", "id": 2, "result": {"sessions": []}}],
    )

    await client.list_sessions()

    assert _last_request(process)["params"] == {}

    await client.stop()


# --------------------------------------------------------------------------- #
# session/resume
# --------------------------------------------------------------------------- #


async def test_resume_session_sends_exact_method_and_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities={"sessionCapabilities": {"resume": {}, "additionalDirectories": {}}},
        response_payloads=[_result("resumed-1")],
    )

    await client.resume_session(
        "sess-9",
        cwd="/repo",
        additional_directories=["/repo/extra"],
        model="m1",
        reasoning_effort="high",
    )

    request = _last_request(process)
    assert request["method"] == "session/resume"
    assert request["params"] == {
        "cwd": "/repo",
        "mcpServers": [],
        "sessionId": "sess-9",
        "additionalDirectories": ["/repo/extra"],
        "model": "m1",
        "reasoningEffort": "high",
    }
    # Granted directories are tracked as session roots (set_roots path).
    assert "/repo/extra" in client._session_state.root_uris

    await client.stop()


# --------------------------------------------------------------------------- #
# session/close (distinct from the session/cancel notification)
# --------------------------------------------------------------------------- #


async def test_close_session_sends_request_not_cancel_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities={"sessionCapabilities": {"close": {}}},
        response_payloads=[{"jsonrpc": "2.0", "id": 2, "result": None}],
    )

    result = await client.close_session("sess-c")

    request = _last_request(process)
    assert request["method"] == "session/close"
    # A request carries an id; the session/cancel interrupt is a bare notification.
    assert "id" in request
    assert request["params"] == {"sessionId": "sess-c"}
    assert result == {}

    await client.stop()


# --------------------------------------------------------------------------- #
# session/delete
# --------------------------------------------------------------------------- #


async def test_delete_session_sends_exact_method_and_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities={"sessionCapabilities": {"delete": {}}},
        response_payloads=[{"jsonrpc": "2.0", "id": 2, "result": {}}],
    )

    await client.delete_session("sess-d")

    request = _last_request(process)
    assert request["method"] == "session/delete"
    assert request["params"] == {"sessionId": "sess-d"}

    await client.stop()


# --------------------------------------------------------------------------- #
# Capability gating
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("call_name", "method"),
    [
        ("list_sessions", "session/list"),
        ("resume_session", "session/resume"),
        ("close_session", "session/close"),
        ("delete_session", "session/delete"),
    ],
)
async def test_lifecycle_methods_raise_when_capability_ungated(
    monkeypatch: pytest.MonkeyPatch,
    call_name: str,
    method: str,
) -> None:
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities={"sessionCapabilities": {}},
    )

    with pytest.raises(UnsupportedACPMethodError) as excinfo:
        if call_name == "list_sessions":
            await client.list_sessions()
        else:
            await getattr(client, call_name)("sess-x")

    assert excinfo.value.method == method
    # Nothing was sent on the wire beyond the initialize handshake.
    assert [message["method"] for message in _written_messages(process)] == ["initialize"]

    await client.stop()


# --------------------------------------------------------------------------- #
# additionalDirectories threading into session/new + session/load
# --------------------------------------------------------------------------- #


async def test_create_session_includes_additional_directories_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities={"sessionCapabilities": {"additionalDirectories": {}}},
        response_payloads=[_result("new-1")],
    )

    await client.create_session(cwd="/repo", additional_directories=["/repo/pkg", "/repo/docs"])

    request = _last_request(process)
    assert request["method"] == "session/new"
    assert request["params"]["cwd"] == "/repo"
    assert request["params"]["mcpServers"] == []
    assert request["params"]["additionalDirectories"] == ["/repo/pkg", "/repo/docs"]
    assert "/repo/pkg" in client._session_state.root_uris
    assert "/repo/docs" in client._session_state.root_uris

    await client.stop()


async def test_create_session_omits_additional_directories_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities={"sessionCapabilities": {"additionalDirectories": {}}},
        response_payloads=[_result("new-2")],
    )

    # Supported capability but an empty list → omitted (spec: omitted == empty).
    await client.create_session(cwd="/repo", additional_directories=[])

    assert "additionalDirectories" not in _last_request(process)["params"]

    await client.stop()


async def test_create_session_omits_additional_directories_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities={"sessionCapabilities": {}},
        response_payloads=[_result("new-3")],
    )

    # Non-empty list but the capability is ungated → omitted, no leak on the wire.
    await client.create_session(cwd="/repo", additional_directories=["/repo/pkg"])

    assert "additionalDirectories" not in _last_request(process)["params"]

    await client.stop()


async def test_load_session_includes_additional_directories_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities={
            "loadSession": True,
            "sessionCapabilities": {"additionalDirectories": {}},
        },
        response_payloads=[_result("loaded-1")],
    )

    await client.load_session("sess-1", cwd="/repo", additional_directories=["/repo/extra"])

    request = _last_request(process)
    assert request["method"] == "session/load"
    assert request["params"]["sessionId"] == "sess-1"
    assert request["params"]["additionalDirectories"] == ["/repo/extra"]

    await client.stop()


# --------------------------------------------------------------------------- #
# Backend exposure (session_capabilities + delegating lifecycle methods)
# --------------------------------------------------------------------------- #


async def test_backend_exposes_session_capabilities_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities={"sessionCapabilities": {"list": {}, "close": {}}},
        response_payloads=[
            {"jsonrpc": "2.0", "id": 2, "result": {"sessions": [{"sessionId": "s1"}]}}
        ],
    )
    backend = _StubACPBackend(client=client)

    assert backend.session_capabilities == {
        "list": True,
        "resume": False,
        "close": True,
        "delete": False,
        "additional_directories": False,
    }

    result = await backend.list_sessions(cwd="/repo")

    assert _last_request(process)["method"] == "session/list"
    assert result == {"sessions": [{"sessionId": "s1"}]}

    await client.stop()


# --------------------------------------------------------------------------- #
# attach_session resume/load/new selection (prefers session/resume)
# --------------------------------------------------------------------------- #


def _fake_session(*, sdk_session_id: str | None) -> SimpleNamespace:
    """A minimal stand-in for ACPManagedChatSession for attach_session.

    Only the attributes attach_session reads/writes are provided.
    """
    return SimpleNamespace(
        _model="m1",
        sdk_session_id=sdk_session_id,
        resume_session_id=None,
        project_path="/repo",
        reasoning_effort=None,
        available_commands=None,
        _connected=False,
        last_activity=None,
    )


async def _attach_with_capabilities(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_capabilities: dict[str, Any],
    sdk_session_id: str | None,
) -> _FakeProcess:
    """Drive attach_session on a started stub backend and return its process."""
    monkeypatch.setattr(
        "gobby.servers.websocket.chat.backends.acp.pre_approve_directory",
        lambda *_a, **_k: None,
    )
    client, process = await _start_client(
        monkeypatch,
        agent_capabilities=agent_capabilities,
        response_payloads=[_result("attached-1")],
    )
    backend = _StubACPBackend(client=client)
    await backend.attach_session(_fake_session(sdk_session_id=sdk_session_id))
    await client.stop()
    return process


async def test_attach_session_prefers_resume_when_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = await _attach_with_capabilities(
        monkeypatch,
        agent_capabilities={"loadSession": True, "sessionCapabilities": {"resume": {}}},
        sdk_session_id="sess-1",
    )

    # resume wins even when loadSession is also advertised.
    assert _last_request(process)["method"] == "session/resume"


async def test_attach_session_falls_back_to_load_when_only_load_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = await _attach_with_capabilities(
        monkeypatch,
        agent_capabilities={"loadSession": True, "sessionCapabilities": {}},
        sdk_session_id="sess-1",
    )

    assert _last_request(process)["method"] == "session/load"


async def test_attach_session_starts_new_when_neither_resume_nor_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stored session id is present, but neither capability is advertised, so
    # the selection starts a fresh session rather than resuming or loading.
    process = await _attach_with_capabilities(
        monkeypatch,
        agent_capabilities={"sessionCapabilities": {}},
        sdk_session_id="sess-1",
    )

    assert _last_request(process)["method"] == "session/new"
