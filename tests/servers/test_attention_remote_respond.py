"""Attention respond on an entry whose terminal lives on another machine."""

from __future__ import annotations

import asyncio
from typing import Any, NamedTuple, cast
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.app_context import ServiceContainer
from gobby.config.bootstrap import BootstrapConfig
from gobby.hub import remote_terminal
from gobby.hub.node_client import NodeCommandError
from gobby.servers.http import HTTPServer
from gobby.storage.attention import AttentionState, AttentionStateManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
from tests.servers.conftest import OTHER_USER_ID, authenticate_as
from tests.servers.test_attention_respond import (
    APPROVAL_PROMPT,
    DETECTION_REGISTRY,
    TEST_USER_ID,
    _manager,
    _open_prompt,
    _request,
)
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit

REMOTE_MACHINE = "30000000-0000-4000-8000-000000000003"


class _FakeNode:
    """Scripted node that records every terminal.write and can refuse or lose it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        self.error: NodeCommandError | None = None

    async def send(
        self,
        machine_id: str,
        command: str,
        payload: dict[str, Any],
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        self.sent.append((machine_id, command, payload))
        # Yield once so a concurrent respond can reach the entry lock mid-write.
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return {}


class _Remote(NamedTuple):
    server: HTTPServer
    manager: AttentionStateManager
    state: AttentionState
    terminal_id: str
    runtime: FakeRuntime


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> _FakeNode:
    fake = _FakeNode()
    monkeypatch.setattr(remote_terminal, "node_command_client", lambda: fake)
    return fake


def _remote_entry(temp_db: HubDatabase) -> _Remote:
    """Open an episode on a terminal that belongs to REMOTE_MACHINE, owned by TEST_USER_ID."""
    LocalMachineManager(temp_db).upsert_seen(REMOTE_MACHINE, TEST_USER_ID)
    manager = _manager(temp_db)
    state = _open_prompt(manager)

    terminal = make_memory_terminal(backend="native", machine_id=REMOTE_MACHINE)
    terminal.session_id = "session-1"
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime(backend="native", snapshot_text=APPROVAL_PROMPT)
    registry = runtime_registry(runtime)
    services = ServiceContainer(
        database=temp_db,
        session_manager=MagicMock(),
        task_manager=MagicMock(),
        attention_manager=manager,
        detection_registry=cast(DetectionManifestRegistry, DETECTION_REGISTRY),
        terminal_manager=cast(Any, store),
        terminal_runtime_registry=registry,
        write_coordinator=WriteCoordinator(
            cast(UnresolvedWriteStore, store),
            registry,
            lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
        ),
    )
    server = HTTPServer(services=services, test_mode=True, bootstrap_config=BootstrapConfig())
    authenticate_as(server, TEST_USER_ID)
    return _Remote(server, manager, state, terminal.id, runtime)


def _respond_url(state: AttentionState) -> str:
    return f"/api/attention/{state.entry_id}/respond"


def test_remote_respond_writes_through_node(
    temp_db: HubDatabase,
    node: _FakeNode,
) -> None:
    remote = _remote_entry(temp_db)

    response = TestClient(remote.server.app).post(
        _respond_url(remote.state),
        json=_request(remote.state, {"option": 1}),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "entry_id": remote.state.entry_id}
    assert node.sent == [
        (
            REMOTE_MACHINE,
            "terminal.write",
            {"terminal_id": remote.terminal_id, "kind": "text", "data": "1", "submit": True},
        )
    ]
    cleared = remote.manager.get(remote.state.entry_id)
    assert cleared is not None
    assert cleared.state is None
    assert remote.runtime.snapshot_modes == []
    assert remote.runtime.write_log == []


def test_offline_owner_is_typed(
    temp_db: HubDatabase,
    node: _FakeNode,
) -> None:
    remote = _remote_entry(temp_db)
    node.error = NodeCommandError("node_offline", stage="none")

    response = TestClient(remote.server.app).post(
        _respond_url(remote.state),
        json=_request(remote.state, {"option": 1}),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "node_offline", "stage": "none"}
    open_entry = remote.manager.get(remote.state.entry_id)
    assert open_entry is not None
    assert open_entry.state == "blocked"
    assert open_entry.attention_id == remote.state.attention_id
    assert len(node.sent) == 1


def test_stale_episode_sends_nothing(
    temp_db: HubDatabase,
    node: _FakeNode,
) -> None:
    remote = _remote_entry(temp_db)
    remote.manager.transition(
        remote.state.entry_id,
        state=None,
        expected_attention_id=remote.state.attention_id,
        expected_fingerprint=remote.state.fingerprint,
    )
    replacement = _open_prompt(remote.manager)

    response = TestClient(remote.server.app).post(
        _respond_url(remote.state),
        json=_request(remote.state, {"option": 1}),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_episode"
    assert response.json()["detail"]["attention_id"] == replacement.attention_id
    assert node.sent == []
    assert remote.runtime.snapshot_modes == []


def test_lost_reply_retires_entry(
    temp_db: HubDatabase,
    node: _FakeNode,
) -> None:
    remote = _remote_entry(temp_db)
    node.error = NodeCommandError("node_timeout", stage=None)
    client = TestClient(remote.server.app)
    body = _request(remote.state, {"option": 1})

    lost = client.post(_respond_url(remote.state), json=body)
    node.error = None
    retry = client.post(_respond_url(remote.state), json=body)

    assert lost.status_code == 502
    assert lost.json()["detail"] == {"code": "injection_indeterminate", "stage": "partial"}
    retired = remote.manager.get(remote.state.entry_id)
    assert retired is not None
    assert retired.state is None
    assert remote.runtime.snapshot_modes == []
    assert retry.status_code == 409
    assert len(node.sent) == 1


@pytest.mark.asyncio
async def test_concurrent_responses_send_once(
    temp_db: HubDatabase,
    node: _FakeNode,
) -> None:
    remote = _remote_entry(temp_db)
    body = _request(remote.state, {"option": 1})
    transport = httpx.ASGITransport(app=remote.server.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first, second = await asyncio.gather(
            client.post(_respond_url(remote.state), json=body),
            client.post(_respond_url(remote.state), json=body),
        )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    assert len(node.sent) == 1


def test_respond_needs_owned_machine(
    temp_db: HubDatabase,
    node: _FakeNode,
) -> None:
    remote = _remote_entry(temp_db)
    body = _request(remote.state, {"option": 1})
    before = remote.manager.get(remote.state.entry_id)
    assert before is not None

    authenticate_as(remote.server, OTHER_USER_ID)
    denied = TestClient(remote.server.app).post(_respond_url(remote.state), json=body)

    after = remote.manager.get(remote.state.entry_id)
    assert after is not None
    assert denied.status_code == 403
    assert denied.json()["detail"] == {"code": "machine_not_owned"}
    assert (after.attention_id, after.state, after.payload, after.seen_at) == (
        before.attention_id,
        before.state,
        before.payload,
        before.seen_at,
    )
    assert node.sent == []
    assert remote.runtime.snapshot_modes == []

    authenticate_as(remote.server, TEST_USER_ID)
    accepted = TestClient(remote.server.app).post(_respond_url(remote.state), json=body)

    assert accepted.status_code == 200
    assert len(node.sent) == 1
    assert remote.runtime.snapshot_modes == []
