"""Codex half of plan row 3.1.7: session-owned app-server clients at the launch seam."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_resolvers import CodexSandboxResolver
from gobby.config.app import DaemonConfig
from gobby.servers.websocket.chat._session_launch import (
    SessionLaunchContext,
    start_hydrated_session,
)
from gobby.servers.websocket.chat.backends.codex import (
    _CODEX_WEB_CHAT_APPROVAL_POLICY,
    CodexManagedChatSession,
    CodexWebChatBackend,
)
from gobby.servers.websocket.chat.runtime_manager import (
    SandboxPolicySnapshot,
    WebChatRuntimeManager,
)

pytestmark = pytest.mark.unit

_CODEX_MOD = "gobby.servers.websocket.chat.backends.codex"


class _SeamCodexClient:
    """Fake shaped like the Codex app-server client ``attach_session`` drives."""

    def __init__(
        self,
        registry: list[_SeamCodexClient],
        *,
        fail_start: bool = False,
        fail_resume_once: bool = False,
    ) -> None:
        self.fail_start = fail_start
        self.fail_resume_once = fail_resume_once
        self.is_connected = False
        self.starts = 0
        self.stopped = False
        self.started_threads: list[dict[str, Any]] = []
        self.resumed_threads: list[str] = []
        registry.append(self)

    async def start(self) -> None:
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("codex app-server launch failed")
        self.is_connected = True

    async def stop(self) -> None:
        self.stopped = True
        self.is_connected = False

    async def start_thread(self, **kwargs: Any) -> SimpleNamespace:
        self.started_threads.append(dict(kwargs))
        thread_id = f"thread-{len(self.started_threads)}-{Path(kwargs['cwd']).name}"
        return SimpleNamespace(id=thread_id, path=f"/transcripts/{thread_id}.jsonl")

    async def resume_thread(self, thread_id: str) -> SimpleNamespace:
        if self.fail_resume_once:
            self.fail_resume_once = False
            raise RuntimeError("thread not found")
        self.resumed_threads.append(thread_id)
        return SimpleNamespace(id=thread_id, path=None)


def _owner() -> SimpleNamespace:
    return SimpleNamespace(
        _chat_sessions={},
        clients={},
        _fire_lifecycle=AsyncMock(return_value=None),
        web_chat_session_registry=None,
    )


def _srt_context(workspace: Path, *domains: str) -> SessionLaunchContext:
    config = SandboxConfig(
        enabled=True, backend="srt", allow_network=False, allowed_domains=list(domains)
    )
    return SessionLaunchContext(
        sandbox=SandboxPolicySnapshot(config=config, policy_hash=f"hash-{'-'.join(domains)}"),
        workspace_path=str(workspace),
    )


def _native_context(workspace: Path) -> SessionLaunchContext:
    config = SandboxConfig(enabled=True, backend="provider-native", mode="permissive")
    return SessionLaunchContext(
        sandbox=SandboxPolicySnapshot(config=config, policy_hash="hash-native"),
        workspace_path=str(workspace),
    )


async def _seam_start(owner: Any, session: Any, context: SessionLaunchContext) -> Any:
    return await start_hydrated_session(
        owner,
        session,
        context,
        session_key=session.conversation_id,
        effective_model="gpt-5.4",
        persona_selected=False,
        pending_agent=None,
        pending_mode="plan",
        agent_name="codex",
        provider_name="codex",
        session_manager=None,
        existing_db_session=None,
        project_context_changed=False,
        effective_pid="proj",
    )


class TestCodexSubprocessesAreSessionOwned:
    @pytest.fixture
    def codex(self) -> Any:
        clients: list[_SeamCodexClient] = []
        plan: list[dict[str, bool]] = []

        def factory(**_: Any) -> _SeamCodexClient:
            options = plan.pop(0) if plan else {}
            return _SeamCodexClient(clients, **options)

        manager = WebChatRuntimeManager(
            codex_client=None,
            # The seam fake stands in for the real client at the injection boundary.
            codex_client_factory=cast(Callable[..., CodexAppServerClient], factory),
            daemon_config=DaemonConfig(),
        )
        backend = manager._codex_backend
        assert isinstance(backend, CodexWebChatBackend)
        return SimpleNamespace(manager=manager, backend=backend, clients=clients, plan=plan)

    @pytest.fixture(autouse=True)
    def _codex_on_path(self) -> Any:
        with patch(f"{_CODEX_MOD}.shutil.which", return_value="/bin/codex"):
            yield

    async def _session(self, codex: Any, conversation_id: str) -> CodexManagedChatSession:
        session = await codex.manager.create_session(
            provider="codex", conversation_id=conversation_id, model="gpt-5.4"
        )
        assert isinstance(session, CodexManagedChatSession)
        session.db_session_id = f"db-{conversation_id}"
        return session

    @pytest.mark.asyncio
    async def test_create_session_launches_nothing(self, codex: Any) -> None:
        await self._session(codex, "conv-idle")

        assert codex.clients == []
        assert codex.backend.client is None

    @pytest.mark.asyncio
    async def test_first_start_launches_under_final_path_and_policy(
        self, codex: Any, tmp_path: Path
    ) -> None:
        session = await self._session(codex, "conv-first")
        owner = _owner()
        workspace = tmp_path / "wt-a"
        workspace.mkdir()

        await _seam_start(owner, session, _srt_context(workspace, "a.test"))

        assert owner._chat_sessions["conv-first"] is session
        assert len(codex.clients) == 1
        client = codex.clients[0]
        assert client.starts == 1
        assert session._app_client is client
        assert client.resumed_threads == []
        assert len(client.started_threads) == 1
        thread = client.started_threads[0]
        assert thread["cwd"] == str(workspace)
        assert thread["model"] == "gpt-5.4"
        assert thread["approval_policy"] == _CODEX_WEB_CHAT_APPROVAL_POLICY
        assert thread["sandbox"] == "danger-full-access"
        assert thread["terminal_context"] == {
            "gobby_session_id": "db-conv-first",
            "gobby_web_chat_child": "1",
        }
        assert session.is_connected is True
        assert session.sdk_session_id == session._thread_id == "thread-1-wt-a"
        assert session._transcript_path == "/transcripts/thread-1-wt-a.jsonl"
        assert codex.backend._sessions_by_thread["thread-1-wt-a"] is session

    @pytest.mark.asyncio
    async def test_resume_reuses_persisted_thread(self, codex: Any, tmp_path: Path) -> None:
        session = await self._session(codex, "conv-resume")
        session.resume_session_id = "thread-old"

        await _seam_start(_owner(), session, _srt_context(tmp_path, "a.test"))

        client = codex.clients[0]
        assert client.resumed_threads == ["thread-old"]
        assert client.started_threads == []
        assert session.sdk_session_id == "thread-old"
        assert codex.backend._sessions_by_thread["thread-old"] is session

    @pytest.mark.asyncio
    async def test_failed_resume_falls_back_to_fresh_thread_on_same_client(
        self, codex: Any, tmp_path: Path
    ) -> None:
        session = await self._session(codex, "conv-fallback")
        session.resume_session_id = "thread-old"
        codex.plan.append({"fail_resume_once": True})

        await _seam_start(_owner(), session, _srt_context(tmp_path, "a.test"))

        assert len(codex.clients) == 1
        client = codex.clients[0]
        assert client.resumed_threads == []
        assert len(client.started_threads) == 1
        assert client.started_threads[0]["cwd"] == str(tmp_path)
        assert session.resume_session_id is None
        assert session.is_connected is True

    @pytest.mark.asyncio
    async def test_failed_start_cleans_up_and_registers_nothing(
        self, codex: Any, tmp_path: Path
    ) -> None:
        session = await self._session(codex, "conv-fail")
        codex.plan.append({"fail_start": True})
        owner = _owner()

        with pytest.raises(RuntimeError, match="app-server launch failed"):
            await _seam_start(owner, session, _srt_context(tmp_path, "a.test"))

        assert owner._chat_sessions == {}
        assert session._app_client is None
        assert session.is_connected is False
        assert session._thread_id is None
        assert codex.backend._sessions_by_thread == {}
        assert all(client.started_threads == [] for client in codex.clients)
        owner._fire_lifecycle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_teardown_stops_the_session_owned_client(
        self, codex: Any, tmp_path: Path
    ) -> None:
        session = await self._session(codex, "conv-teardown")
        await _seam_start(_owner(), session, _srt_context(tmp_path, "a.test"))
        client = codex.clients[0]
        thread_id = session._thread_id
        assert client.stopped is False

        await session.stop()

        assert client.stopped is True
        assert client.is_connected is False
        assert session.is_connected is False
        assert session._app_client is None
        assert thread_id not in codex.backend._sessions_by_thread

    @pytest.mark.asyncio
    async def test_concurrent_sessions_get_distinct_confinement_roots(
        self, codex: Any, tmp_path: Path
    ) -> None:
        session_a = await self._session(codex, "conv-a")
        session_b = await self._session(codex, "conv-b")
        worktree_a = tmp_path / "project-a" / "wt"
        worktree_b = tmp_path / "project-b" / "wt"
        worktree_a.mkdir(parents=True)
        worktree_b.mkdir(parents=True)
        owner = _owner()
        native = _native_context(worktree_b)

        await asyncio.gather(
            _seam_start(owner, session_a, _srt_context(worktree_a, "a.test")),
            _seam_start(owner, session_b, native),
        )

        assert set(owner._chat_sessions) == {"conv-a", "conv-b"}
        assert len(codex.clients) == 2
        assert session_a._app_client is not session_b._app_client
        client_a = next(client for client in codex.clients if client is session_a._app_client)
        client_b = next(client for client in codex.clients if client is session_b._app_client)
        thread_a = client_a.started_threads[0]
        thread_b = client_b.started_threads[0]
        assert thread_a["cwd"] == str(worktree_a)
        assert thread_b["cwd"] == str(worktree_b)
        assert thread_a["cwd"] != thread_b["cwd"]
        assert thread_a["sandbox"] == "danger-full-access"
        assert thread_b["sandbox"] == CodexSandboxResolver.thread_sandbox_policy(
            native.sandbox.config
        )
        assert thread_a["sandbox"] != thread_b["sandbox"]
        assert thread_a["terminal_context"]["gobby_session_id"] == "db-conv-a"
        assert thread_b["terminal_context"]["gobby_session_id"] == "db-conv-b"
        assert session_a.sandbox_config.allowed_domains == ["a.test"]
        assert session_b.sandbox_config.backend == "provider-native"
        assert codex.backend._sessions_by_thread == {
            session_a._thread_id: session_a,
            session_b._thread_id: session_b,
        }
