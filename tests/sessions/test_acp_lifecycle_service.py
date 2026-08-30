"""Unit tests for lifecycle operations on genuine Gobby ACP sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest

from gobby.adapters.acp_client import ACPClient
from gobby.sessions.acp_lifecycle import (
    ACPCapabilityUnsupportedError,
    ACPProviderUnavailableError,
    ACPSessionLifecycleService,
    ACPSessionNotFoundError,
    ACPTargetNotSupportedError,
    ACPWorkspaceIdentityError,
)

pytestmark = pytest.mark.unit


@dataclass
class _FakeSession:
    id: str = "sess-1"
    external_id: str = "acp-session-xyz"
    machine_id: str = "21000000-0000-4000-8000-000000000001"
    source: str = "qwen"
    project_id: str = "proj-1"
    title: str | None = "Work"
    title_source: str | None = "manual"
    status: str = "active"
    session_type: str = "web_chat"
    workspace_path: str | None = "/tmp/acp-workspace"
    workspace_generation: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "external_id": self.external_id,
            "machine_id": self.machine_id,
            "source": self.source,
            "project_id": self.project_id,
            "title": self.title,
            "title_source": self.title_source,
            "status": self.status,
            "session_type": self.session_type,
        }


class _FakeSessionManager:
    def __init__(self, session: _FakeSession | None = None) -> None:
        self.rows = {session.id: session} if session else {}
        self.events: list[tuple[str, str]] = []
        self.delete_error: Exception | None = None
        self.delete_result: bool | None = None

    def get(self, session_id: str) -> _FakeSession | None:
        return self.rows.get(session_id)

    def update_status(self, session_id: str, status: str) -> _FakeSession | None:
        session = self.rows.get(session_id)
        if session is None:
            return None
        session.status = status
        self.events.append(("session_expired", session_id))
        return session

    def delete(self, session_id: str) -> bool:
        if self.delete_error is not None:
            raise self.delete_error
        if self.delete_result is not None:
            return self.delete_result
        if session_id not in self.rows:
            return False
        del self.rows[session_id]
        self.events.append(("session_deleted", session_id))
        return True


class _FakeACPClient:
    def __init__(self, cwd: str | None = None, **_kwargs: Any) -> None:
        self.cwd = cwd
        self.started = False
        self.stopped = False
        self.closed: list[str] = []
        self.deleted: list[str] = []
        self.session_capabilities: dict[str, bool] = {}

    async def start(self, **_kwargs: Any) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def close_session(self, session_id: str) -> dict[str, Any]:
        self.closed.append(session_id)
        return {}

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        self.deleted.append(session_id)
        return {}


class _FakeBackend:
    def __init__(
        self,
        *,
        available: bool = True,
        capabilities: dict[str, bool] | None = None,
    ) -> None:
        self._available = available
        self.capabilities = capabilities or {}
        self.clients: list[_FakeACPClient] = []

        def _factory(*_args: Any, **kwargs: Any) -> _FakeACPClient:
            client = _FakeACPClient(**kwargs)
            client.session_capabilities = dict(self.capabilities)
            self.clients.append(client)
            return client

        self.acp_client_cls = _factory

    def health(self) -> SimpleNamespace:
        return SimpleNamespace(available=self._available)

    @property
    def closed(self) -> list[str]:
        return [item for client in self.clients for item in client.closed]

    @property
    def deleted(self) -> list[str]:
        return [item for client in self.clients for item in client.deleted]


class _FakeRuntimeManager:
    def __init__(self, backend: _FakeBackend) -> None:
        self.backend = backend

    def acp_backends(self) -> dict[str, _FakeBackend]:
        return {"qwen": self.backend}

    def acp_backend(self, provider: str) -> _FakeBackend | None:
        return self.backend if provider == "qwen" else None

    def acp_session_capabilities(self, provider: str) -> dict[str, bool]:
        return dict(self.backend.capabilities) if provider == "qwen" else {}

    def get_acp_session_info(self, _provider: str, _session_id: str) -> None:
        return None


def _service(
    session_manager: _FakeSessionManager,
    backend: _FakeBackend,
) -> ACPSessionLifecycleService:
    return ACPSessionLifecycleService(
        session_manager=session_manager,
        runtime_manager=_FakeRuntimeManager(backend),
    )


@pytest.mark.asyncio
async def test_close_transitions_genuine_session_to_expired() -> None:
    session_manager = _FakeSessionManager(_FakeSession())
    backend = _FakeBackend(capabilities={"close": True})

    result = await _service(session_manager, backend).close("sess-1")

    assert backend.closed == ["acp-session-xyz"]
    assert session_manager.events == [("session_expired", "sess-1")]
    assert result["session"]["status"] == "expired"


@pytest.mark.asyncio
async def test_close_rejects_unknown_non_acp_and_unavailable_targets() -> None:
    backend = _FakeBackend(capabilities={"close": True})
    with pytest.raises(ACPSessionNotFoundError):
        await _service(_FakeSessionManager(), backend).close("missing")

    non_acp = _FakeSession(source="claude")
    with pytest.raises(ACPTargetNotSupportedError):
        await _service(_FakeSessionManager(non_acp), backend).close("sess-1")

    unavailable = _FakeBackend(available=False, capabilities={"close": True})
    with pytest.raises(ACPProviderUnavailableError):
        await _service(_FakeSessionManager(_FakeSession()), unavailable).close("sess-1")


@pytest.mark.asyncio
async def test_close_requires_advertised_capability() -> None:
    backend = _FakeBackend()

    with pytest.raises(ACPCapabilityUnsupportedError):
        await _service(_FakeSessionManager(_FakeSession()), backend).close("sess-1")

    assert backend.closed == []


@pytest.mark.asyncio
async def test_close_fails_closed_without_workspace_identity() -> None:
    session_manager = _FakeSessionManager(_FakeSession(workspace_path=None))
    backend = _FakeBackend(capabilities={"close": True})

    with pytest.raises(ACPWorkspaceIdentityError):
        await _service(session_manager, backend).close("sess-1")

    assert backend.clients == []
    assert backend.closed == []


@pytest.mark.asyncio
async def test_delete_removes_genuine_session() -> None:
    session_manager = _FakeSessionManager(_FakeSession())
    backend = _FakeBackend(capabilities={"delete": True})

    result = await _service(session_manager, backend).delete("sess-1")

    assert backend.deleted == ["acp-session-xyz"]
    assert session_manager.events == [("session_deleted", "sess-1")]
    assert result["disposition"] == "removed"


@pytest.mark.asyncio
async def test_delete_raises_when_storage_delete_reports_missing() -> None:
    session_manager = _FakeSessionManager(_FakeSession())
    session_manager.delete_result = False
    backend = _FakeBackend(capabilities={"delete": True})

    with pytest.raises(ACPSessionNotFoundError):
        await _service(session_manager, backend).delete("sess-1")

    assert backend.deleted == ["acp-session-xyz"]


@pytest.mark.asyncio
async def test_delete_fk_failure_expires_session() -> None:
    session_manager = _FakeSessionManager(_FakeSession())
    session_manager.delete_error = psycopg.IntegrityError("session still referenced")
    backend = _FakeBackend(capabilities={"delete": True})

    result = await _service(session_manager, backend).delete("sess-1")

    assert backend.deleted == ["acp-session-xyz"]
    assert session_manager.events == [("session_expired", "sess-1")]
    assert result["disposition"] == "expired"


@pytest.mark.asyncio
async def test_delete_requires_advertised_capability() -> None:
    backend = _FakeBackend()

    with pytest.raises(ACPCapabilityUnsupportedError):
        await _service(_FakeSessionManager(_FakeSession()), backend).delete("sess-1")

    assert backend.deleted == []


class _GenerationBumpManager(_FakeSessionManager):
    def __init__(self, session: _FakeSession, *, bump_on_get: int) -> None:
        super().__init__(session)
        self._bump_on_get = bump_on_get
        self._gets = 0

    def get(self, session_id: str) -> _FakeSession | None:
        self._gets += 1
        session = self.rows.get(session_id)
        if session is not None and self._gets >= self._bump_on_get:
            session.workspace_generation += 1
        return session


@pytest.mark.asyncio
async def test_close_aborts_without_subprocess_when_generation_changes_before_start() -> None:
    session = _FakeSession()
    session_manager = _GenerationBumpManager(session, bump_on_get=2)
    backend = _FakeBackend(capabilities={"close": True})

    with pytest.raises(ACPWorkspaceIdentityError, match="before launch"):
        await _service(session_manager, backend).close("sess-1")

    assert backend.clients == []
    assert backend.closed == []


class _StubACPClient(ACPClient):
    cli_name = "stub-acp"
    display_name = "Stub ACP"
    prompt_timeout_env = "GOBBY_STUB_ACP_PROMPT_TIMEOUT_SECONDS"


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
        self.terminated = False

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


def _written_methods(process: _FakeProcess) -> list[str]:
    return [
        json.loads(line)["method"]
        for line in process.stdin.buffer.decode().splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_close_does_not_handshake_when_generation_changes_at_process_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    session_manager = _FakeSessionManager(session)
    process = _FakeProcess(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {"sessionCapabilities": {"close": True}},
                },
            }
        ]
    )

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        session.workspace_generation += 1
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    backend = _FakeBackend(capabilities={"close": True})

    def _make_client(**kwargs: Any) -> _StubACPClient:
        return _StubACPClient(cli_path="/usr/bin/stub-acp", **kwargs)

    cast(Any, backend).acp_client_cls = _make_client

    with pytest.raises(ACPWorkspaceIdentityError, match="during launch"):
        await _service(session_manager, backend).close("sess-1")

    assert _written_methods(process) == []
    assert process.terminated is True
