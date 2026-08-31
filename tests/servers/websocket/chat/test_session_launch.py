"""Post-hydration launch seam: lifecycle binding and per-launch policy."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.config.app import DaemonConfig
from gobby.hooks.events import HookEventType
from gobby.servers.websocket.chat._session_launch import (
    NATIVE_HOOK_AUTHORITY_PROVIDERS,
    SessionLaunchContext,
    _cleanup_failed_start,
    bind_session_lifecycle,
    start_hydrated_session,
    uses_native_hook_authority,
)
from gobby.servers.websocket.chat.backends.agy import AgyManagedChatSession
from gobby.servers.websocket.chat.backends.droid import DroidManagedChatSession
from gobby.servers.websocket.chat.runtime_manager import (
    SandboxPolicySnapshot,
    WebChatRuntimeManager,
)
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit

_LIFECYCLE_CALLBACKS = (
    "_on_before_agent",
    "_on_pre_tool",
    "_on_post_tool",
    "_on_pre_compact",
    "_on_stop",
)


def _owner() -> SimpleNamespace:
    return SimpleNamespace(
        _chat_sessions={},
        clients={},
        _fire_lifecycle=AsyncMock(return_value=None),
        web_chat_session_registry=None,
    )


class TestAgySingleLifecycleAuthority:
    def test_registry_names_agy_only(self) -> None:
        assert NATIVE_HOOK_AUTHORITY_PROVIDERS == frozenset({"agy"})
        assert uses_native_hook_authority(AgyManagedChatSession(conversation_id="c"))
        assert not uses_native_hook_authority(DroidManagedChatSession(conversation_id="c"))

    def test_bind_leaves_agy_lifecycle_callbacks_unbound(self) -> None:
        owner = _owner()
        session = AgyManagedChatSession(conversation_id="conv-agy")

        bind_session_lifecycle(owner, session, "conv-agy")

        for name in _LIFECYCLE_CALLBACKS:
            assert getattr(session, name) is None, name
        assert session._on_mode_changed is not None
        assert session._on_plan_ready is not None

    @pytest.mark.asyncio
    async def test_bind_wires_incumbent_lifecycle_callbacks(self) -> None:
        owner = _owner()
        session = DroidManagedChatSession(conversation_id="conv-droid")

        bind_session_lifecycle(owner, session, "conv-droid")

        for name in _LIFECYCLE_CALLBACKS:
            assert callable(getattr(session, name)), name
        assert session._on_pre_tool is not None
        await session._on_pre_tool({"tool_name": "Bash"})
        owner._fire_lifecycle.assert_awaited_once_with(
            "conv-droid", HookEventType.BEFORE_TOOL, {"tool_name": "Bash"}
        )

    @pytest.mark.asyncio
    async def test_agy_bound_session_never_reaches_fire_lifecycle(self) -> None:
        owner = _owner()
        session = AgyManagedChatSession(conversation_id="conv-agy")
        bind_session_lifecycle(owner, session, "conv-agy")

        assert await session._apply_pre_tool_lifecycle("Write", {}) is None
        assert await session._apply_post_tool_lifecycle("Write", {}, "ok") is None
        owner._fire_lifecycle.assert_not_awaited()


class TestFailedStartCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_removes_shim_then_stops_session(self) -> None:
        order: list[str] = []
        launch = SimpleNamespace(cleanup_cli_shim=lambda: order.append("shim"))
        session = SimpleNamespace(
            _sandbox_launch=launch,
            conversation_id="conv-1",
            stop=AsyncMock(side_effect=lambda: order.append("stop")),
        )

        await _cleanup_failed_start(session)

        assert order == ["shim", "stop"]


def _fake_launching_session(conversation_id: str, gate: asyncio.Event | None) -> Any:
    session = DroidManagedChatSession(conversation_id=conversation_id)
    launches: list[dict[str, Any]] = []

    async def _start(model: str | None = None) -> None:
        if gate is not None:
            await gate.wait()
        launches.append(
            {
                "model": model,
                "project_path": session.project_path,
                "allowed_domains": list(session.sandbox_config.allowed_domains),
                "policy_hash": session.sandbox_policy_hash,
            }
        )
        session.sandbox_metadata = {
            "backend": "srt",
            "enforced": True,
            "policy_hash": session.sandbox_policy_hash,
        }
        session._connected = True

    session.start = _start  # type: ignore[method-assign]
    session.stop = AsyncMock()  # type: ignore[method-assign]
    session._launches = launches
    return session


def _launch(
    owner: Any,
    session: Any,
    context: SessionLaunchContext,
    *,
    session_manager: Any,
    existing: Any,
) -> Any:
    return start_hydrated_session(
        owner,
        session,
        context,
        session_key=session.conversation_id,
        effective_model=None,
        persona_selected=False,
        pending_agent=None,
        pending_mode="plan",
        agent_name="droid",
        provider_name="droid",
        session_manager=session_manager,
        existing_db_session=existing,
        project_context_changed=False,
        effective_pid="proj",
    )


class TestInterleavedLaunchesPersistOwnPolicy:
    """Plan row 3.1.21: each delayed launch persists its own sandbox policy and hash."""

    @pytest.mark.asyncio
    async def test_delayed_first_launch_persists_its_own_hash(self, tmp_path: Path) -> None:
        holder: dict[str, DaemonConfig] = {"config": DaemonConfig()}
        manager = WebChatRuntimeManager(
            daemon_config=holder["config"],
            config_resolver=lambda: holder["config"],
        )
        snapshot_a = manager._refresh_sandbox_config()
        holder["config"] = DaemonConfig(
            web_chat_sandbox={
                "backend": "srt",
                "allow_network": False,
                "allowed_domains": ["x.test"],
            }
        )
        snapshot_b = manager._refresh_sandbox_config()
        assert snapshot_a.policy_hash != snapshot_b.policy_hash

        gate = asyncio.Event()
        session_a = _fake_launching_session("conv-a", gate)
        session_b = _fake_launching_session("conv-b", None)
        session_a.db_session_id = "db-a"
        session_b.db_session_id = "db-b"
        session_manager = MagicMock()
        session_manager.update = MagicMock(return_value=None)
        owner = _owner()
        existing_a = SimpleNamespace(
            session_type="web_chat", status="active", workspace_path=None, workspace_generation=0
        )
        existing_b = SimpleNamespace(
            session_type="web_chat", status="active", workspace_path=None, workspace_generation=0
        )
        context_a = SessionLaunchContext(sandbox=snapshot_a, workspace_path=str(tmp_path / "a"))
        context_b = SessionLaunchContext(sandbox=snapshot_b, workspace_path=str(tmp_path / "b"))

        task_a = asyncio.create_task(
            _launch(
                owner, session_a, context_a, session_manager=session_manager, existing=existing_a
            )
        )
        await asyncio.sleep(0)
        await _launch(
            owner, session_b, context_b, session_manager=session_manager, existing=existing_b
        )
        assert session_a._launches == []
        gate.set()
        await task_a
        await drain_asyncio_tasks()

        assert session_a._launches[0]["policy_hash"] == snapshot_a.policy_hash
        assert session_a._launches[0]["allowed_domains"] == list(snapshot_a.config.allowed_domains)
        assert session_a._launches[0]["project_path"] == str(tmp_path / "a")
        assert session_b._launches[0]["policy_hash"] == snapshot_b.policy_hash
        assert session_b._launches[0]["allowed_domains"] == ["x.test"]
        persisted = {
            call.args[0]: call.kwargs["sandbox_policy_hash"]
            for call in session_manager.update.call_args_list
        }
        assert persisted == {"db-a": snapshot_a.policy_hash, "db-b": snapshot_b.policy_hash}
        assert owner._chat_sessions == {"conv-a": session_a, "conv-b": session_b}


class TestLaunchContextIsApplied:
    @pytest.mark.asyncio
    async def test_snapshot_config_is_copied_not_shared(self, tmp_path: Path) -> None:
        config = SandboxConfig(enabled=True, backend="srt", allowed_domains=["a.test"])
        snapshot = SandboxPolicySnapshot(config=config, policy_hash="hash-a")
        session = _fake_launching_session("conv-copy", None)
        owner = _owner()

        await _launch(
            owner,
            session,
            SessionLaunchContext(sandbox=snapshot, workspace_path=str(tmp_path)),
            session_manager=None,
            existing=None,
        )
        await drain_asyncio_tasks()

        assert session.sandbox_config == config
        assert session.sandbox_config is not config
        assert session.sandbox_policy_hash == "hash-a"
        assert session.project_path == str(tmp_path)
