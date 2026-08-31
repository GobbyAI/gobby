"""Workspace-identity launch contracts (plan rows 3.1.16, 3.1.21, 3.1.23)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.acp_client import ACPClient
from gobby.agents.sandbox import SandboxConfig
from gobby.servers.websocket.chat._session import ChatSessionMixin
from gobby.servers.websocket.chat._streaming import ChatStreamingMixin
from gobby.sessions.acp_lifecycle import (
    ACPSessionLifecycleService,
    ACPWorkspaceIdentityError,
)

pytestmark = pytest.mark.unit

WORKTREE_A = "/tmp/identity-worktree-a"
WORKTREE_B = "/tmp/identity-worktree-b"


@dataclass
class _Row:
    id: str = "sess-1"
    external_id: str = "acp-session-xyz"
    machine_id: str = "21000000-0000-4000-8000-000000000001"
    source: str = "qwen"
    project_id: str = "proj-1"
    status: str = "active"
    session_type: str = "web_chat"
    workspace_path: str | None = WORKTREE_A
    workspace_generation: int = 3
    sandbox_config: SandboxConfig = field(default_factory=lambda: SandboxConfig(enabled=False))
    sandbox_policy_hash: str = "hash"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.status, "session_type": self.session_type}


Writer = Callable[[_Row], None]


def project_switch(row: _Row) -> None:
    """``handle_set_project``: pause, invalidate the path, bump the generation."""
    row.status = "paused"
    row.project_id = "proj-new"
    row.workspace_path = None
    row.workspace_generation += 1


def worktree_switch(row: _Row) -> None:
    """``handle_set_worktree``: pause, point at the new worktree, bump the generation."""
    row.status = "paused"
    row.workspace_path = WORKTREE_B
    row.workspace_generation += 1


def worktree_deletion(row: _Row) -> None:
    """``LocalWorktreeManager.delete``: tombstone referencing identities."""
    row.workspace_path = None
    row.workspace_generation += 1


WRITERS = {
    "project_switch": project_switch,
    "worktree_switch": worktree_switch,
    "worktree_deletion": worktree_deletion,
}


class _RowStore:
    """Session-manager fake returning fresh snapshots, like a real DB read."""

    def __init__(self, row: _Row) -> None:
        self.row = row
        self.reads = 0
        self.race_on_read: tuple[int, Writer] | None = None
        self.statuses: list[str] = []

    def get(self, session_id: str) -> _Row | None:
        if session_id != self.row.id:
            return None
        self.reads += 1
        if self.race_on_read is not None and self.reads == self.race_on_read[0]:
            self.race_on_read[1](self.row)
        return replace(self.row)

    def update_status(self, session_id: str, status: str) -> _Row | None:
        assert session_id == self.row.id
        self.row.status = status
        self.statuses.append(status)
        return replace(self.row)

    def delete(self, session_id: str) -> bool:
        return session_id == self.row.id


class _RecordingClient:
    def __init__(self, registry: list[_RecordingClient], **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.cwd = kwargs.get("cwd")
        self.started = False
        self.stopped = False
        self.closed: list[str] = []
        self.session_capabilities = {"close": True, "delete": True}
        registry.append(self)

    async def start(self, **kwargs: Any) -> None:
        self.started = True
        after = kwargs.get("after_process_spawned")
        if after is not None:
            await after()

    async def stop(self) -> None:
        self.stopped = True

    async def close_session(self, session_id: str) -> dict[str, Any]:
        self.closed.append(session_id)
        return {}

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        return {}


class _Backend:
    def __init__(self, factory: Callable[..., Any]) -> None:
        self.acp_client_cls = factory
        self.clients: list[Any] = []

    def health(self) -> SimpleNamespace:
        return SimpleNamespace(available=True)


def _runtime(backend: _Backend) -> SimpleNamespace:
    return SimpleNamespace(
        acp_backends=lambda: {"qwen": backend},
        acp_backend=lambda provider: backend if provider == "qwen" else None,
        acp_session_capabilities=lambda provider: {"close": True, "delete": True},
        get_acp_session_info=lambda _p, _s: None,
    )


def _service(store: _RowStore) -> tuple[ACPSessionLifecycleService, list[_RecordingClient]]:
    clients: list[_RecordingClient] = []
    backend = _Backend(lambda **kwargs: _RecordingClient(clients, **kwargs))
    service = ACPSessionLifecycleService(
        session_manager=cast(Any, store),
        runtime_manager=cast(Any, _runtime(backend)),
    )
    return service, clients


class TestRestartBeforeRehydration:
    """Plan row 3.1.16: a persisted stale or tombstoned identity fails closed at use."""

    @pytest.mark.parametrize("writer_name", ["project_switch", "worktree_deletion"])
    @pytest.mark.asyncio
    async def test_invalidated_identity_fails_closed_after_restart(self, writer_name: str) -> None:
        row = _Row()
        WRITERS[writer_name](row)
        assert row.workspace_path is None
        # A daemon restart drops every in-memory pending override; only the row survives.
        store = _RowStore(row)
        service, clients = _service(store)

        with pytest.raises(ACPWorkspaceIdentityError, match="absent"):
            await service.close("sess-1")

        assert clients == []
        assert store.statuses == []

    @pytest.mark.asyncio
    async def test_rehydrated_worktree_switch_uses_persisted_path_only(self) -> None:
        row = _Row()
        worktree_switch(row)
        store = _RowStore(row)
        service, clients = _service(store)

        result = await service.close("sess-1")

        assert [client.cwd for client in clients] == [WORKTREE_B]
        assert clients[0].closed == ["acp-session-xyz"]
        assert clients[0].stopped is True
        assert result["session"]["status"] == "expired"

    @pytest.mark.asyncio
    async def test_stale_generation_snapshot_fails_closed(self) -> None:
        stale = _Row(workspace_generation=2)
        store = _RowStore(_Row(workspace_generation=3))
        service, clients = _service(store)

        with pytest.raises(ACPWorkspaceIdentityError, match="before launch"):
            await service._with_operation_client(
                cast(Any, stale),
                "qwen",
                lambda client: client.close_session("acp-session-xyz"),
                capability="close",
            )

        assert clients == []


class TestForcedRacesPerWriter:
    """Plan row 3.1.23: both re-read windows fail closed for every identity writer."""

    @pytest.mark.parametrize("writer_name", sorted(WRITERS))
    @pytest.mark.asyncio
    async def test_writer_between_snapshot_and_launch_creates_no_subprocess(
        self, writer_name: str
    ) -> None:
        store = _RowStore(_Row())
        # Read 1 is the operation snapshot; read 2 is the pre-launch re-read.
        store.race_on_read = (2, WRITERS[writer_name])
        service, clients = _service(store)

        with pytest.raises(ACPWorkspaceIdentityError, match="before launch"):
            await service.close("sess-1")

        assert clients == []
        assert store.statuses == []
        assert store.row.workspace_generation == 4

    @pytest.mark.parametrize("writer_name", sorted(WRITERS))
    @pytest.mark.asyncio
    async def test_writer_at_process_creation_terminates_child_before_handshake(
        self, writer_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _RowStore(_Row())
        process = _HandshakeProcess()

        async def spawn_and_race(*_args: Any, **kwargs: Any) -> _HandshakeProcess:
            assert kwargs["cwd"] == WORKTREE_A
            WRITERS[writer_name](store.row)
            return process

        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn_and_race)
        backend = _Backend(lambda **kwargs: _StubACPClient(cli_path="/usr/bin/stub-acp", **kwargs))
        service = ACPSessionLifecycleService(
            session_manager=cast(Any, store),
            runtime_manager=cast(Any, _runtime(backend)),
        )

        with pytest.raises(ACPWorkspaceIdentityError, match="during launch"):
            await service.close("sess-1")

        assert process.handshake_methods() == []
        assert process.terminated is True
        assert store.statuses == []

    @pytest.mark.asyncio
    async def test_unchanged_generation_binds_the_client(self) -> None:
        store = _RowStore(_Row())
        service, clients = _service(store)

        await service.close("sess-1")

        assert store.reads >= 3
        assert clients[0].closed == ["acp-session-xyz"]
        assert store.statuses == ["expired"]


class _StubACPClient(ACPClient):
    cli_name = "stub-acp"
    display_name = "Stub ACP"
    prompt_timeout_env = "GOBBY_STUB_ACP_PROMPT_TIMEOUT_SECONDS"


class _HandshakeStdin:
    def __init__(self) -> None:
        self.buffer = b""

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _EmptyStdout:
    async def readline(self) -> bytes:
        return b""


class _EmptyStderr:
    async def read(self, _n: int = -1) -> bytes:
        return b""


class _HandshakeProcess:
    pid = 4321

    def __init__(self) -> None:
        self.stdin = _HandshakeStdin()
        self.stdout = _EmptyStdout()
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

    def handshake_methods(self) -> list[str]:
        return [
            json.loads(line)["method"]
            for line in self.stdin.buffer.decode().splitlines()
            if line.strip()
        ]


class _LaunchMixin(ChatStreamingMixin, ChatSessionMixin):
    def __init__(self) -> None:
        self.clients: dict = {}
        self._chat_sessions: dict = {}
        self._active_chat_tasks: dict = {}
        self._pending_modes: dict = {}
        self._pending_worktree_paths: dict = {}
        self._pending_agents: dict = {}
        self._pending_projects: dict = {}
        self._pending_providers: dict = {}
        self._session_create_locks: dict = {}
        self.session_manager: Any = None
        self.daemon_config: Any = None
        self.web_chat_runtime_manager: Any = None
        self.config_runtime: Any = None

    async def _fire_lifecycle(self, cid: str, event_type: Any, data: object) -> None:
        return None


class TestPendingWorktreeOverrideLaunch:
    """Plan row 3.1.21: a pending worktree override launches under the resolved path."""

    @pytest.mark.asyncio
    async def test_launch_uses_override_not_pre_resolution_project_path(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "feature-wt"
        worktree.mkdir()
        existing = MagicMock()
        existing.id = "db-wt"
        existing.seq_num = 1
        existing.session_type = "web_chat"
        existing.status = "active"
        existing.source = "qwen"
        existing.project_id = "proj-1"
        existing.external_id = None
        existing.usage_output_tokens = 0
        existing.chat_mode = None
        existing.approved_tools_json = None
        existing.workspace_path = "/repo/main"
        existing.workspace_generation = 2
        existing.sandbox_policy_hash = "hash"

        launch_paths: list[str | None] = []
        session = AsyncMock()
        session.provider = "qwen"
        session.chat_mode = "plan"
        session.db_session_id = None
        session.resume_session_id = None
        session.project_path = None
        session.project_id = None
        session.system_prompt_override = None
        session.model = None
        session.sdk_session_id = None
        session._transcript_path = None
        session.sandbox_metadata = {}
        session.sandbox_policy_hash = "hash"
        session.sandbox_config = SandboxConfig(enabled=False)

        async def _start(model: str | None = None) -> None:
            launch_paths.append(session.project_path)

        session.start = AsyncMock(side_effect=_start)

        mixin = _LaunchMixin()
        mixin.web_chat_runtime_manager = MagicMock()
        mixin.web_chat_runtime_manager.create_session.return_value = session
        mixin.web_chat_runtime_manager.policy_mismatch_reason.return_value = None
        mixin.web_chat_runtime_manager.sandbox_policy_hash = "hash"
        mixin.session_manager = MagicMock()
        mixin.session_manager.db = MagicMock()
        mixin.session_manager.get.return_value = existing
        mixin._pending_worktree_paths["conv-wt"] = str(worktree)

        with patch("gobby.storage.projects.LocalProjectManager") as project_manager:
            project_manager.return_value.get.return_value = SimpleNamespace(repo_path="/repo/main")
            await mixin._create_chat_session_inner("conv-wt", provider="qwen")

        assert launch_paths == [str(worktree)]
        assert session.project_path == str(worktree)
        assert "conv-wt" not in mixin._pending_worktree_paths
        update_kwargs = mixin.session_manager.update.call_args.kwargs
        assert update_kwargs["workspace_path"] == str(worktree)
        assert update_kwargs["workspace_generation"] == 3
