"""Launch-contract matrix for web-chat SRT wrapping."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.sandbox import (
    SandboxConfig,
    web_chat_policy_mismatch_message,
    web_chat_sandbox_config,
    web_chat_sandbox_policy_hash,
)
from gobby.agents.srt_runtime import SandboxLaunch
from gobby.config.app import DaemonConfig
from gobby.servers.websocket.chat.runtime_manager import (
    SandboxPolicySnapshot,
    WebChatRuntimeManager,
)

pytestmark = pytest.mark.unit

_PROVIDERS = ("claude", "codex", "droid", "grok", "qwen")


def _srt_launch(*, executable: str = "/bin/provider") -> SandboxLaunch:
    return SandboxLaunch(
        backend="srt",
        enforced=True,
        node_path="/usr/bin/node",
        runner_path="/opt/srt/runner.js",
        policy_path="/tmp/srt-policy.json",
        violation_path="/tmp/srt-violations.jsonl",
        provider_executable=executable,
        provider_env={"TMPDIR": "/tmp/srt-run"},
        policy_hash="srt-hash",
    )


class TestWebChatSandboxDefaults:
    def test_default_is_srt_with_bounded_network_policy(self) -> None:
        resolved = web_chat_sandbox_config(DaemonConfig())

        assert resolved.backend == "srt"
        assert resolved.enabled is True
        assert resolved.allow_network is False
        assert resolved.allow_git_network is True
        assert resolved.allow_package_registries is True


class TestRefreshReturnsImmutableSnapshot:
    def test_refresh_returns_config_and_hash_snapshot(self) -> None:
        daemon = DaemonConfig()
        manager = WebChatRuntimeManager(daemon_config=daemon)
        snapshot = manager._refresh_sandbox_config()
        config = getattr(snapshot, "config", None)
        policy_hash = getattr(snapshot, "policy_hash", None)

        assert isinstance(snapshot, SandboxPolicySnapshot)
        assert config == web_chat_sandbox_config(daemon)
        assert policy_hash == web_chat_sandbox_policy_hash(daemon)
        assert snapshot.config is manager._sandbox_config or snapshot.config == manager._sandbox_config


class TestStalePolicyHashRefusal:
    @pytest.mark.parametrize("provider", _PROVIDERS)
    def test_stale_hash_refuses_resume(self, provider: str) -> None:
        manager = WebChatRuntimeManager(daemon_config=DaemonConfig())
        session = SimpleNamespace(
            session_type="web_chat",
            source=provider,
            sandbox_policy_hash="stale-hash",
        )

        reason = manager.policy_mismatch_reason(session)

        assert reason == web_chat_policy_mismatch_message()


class TestCreateSessionLaunchGate:
    @pytest.mark.asyncio
    async def test_create_session_does_not_start_subprocesses(self) -> None:
        manager = WebChatRuntimeManager(daemon_config=DaemonConfig())
        session = await manager.create_session(provider="claude", conversation_id="conv-1")

        assert session.conversation_id == "conv-1"
        assert isinstance(session.sandbox_policy_hash, str)
        assert len(session.sandbox_policy_hash) == 64
        assert manager._grok_backend._client.is_started is False
        assert manager._qwen_backend._client.is_started is False
        client = manager._codex_backend.client
        assert client is None or client.is_connected is False

    @pytest.mark.asyncio
    async def test_srt_satisfies_sensitive_path_gate_for_every_incumbent(self) -> None:
        manager = WebChatRuntimeManager(daemon_config=DaemonConfig())
        for provider in _PROVIDERS:
            session = await manager.create_session(
                provider=provider, conversation_id=f"conv-{provider}"
            )
            assert session is not None

    @pytest.mark.asyncio
    async def test_provider_native_gate_remains_per_provider(self) -> None:
        daemon = DaemonConfig(web_chat_sandbox={"backend": "provider-native"})
        manager = WebChatRuntimeManager(daemon_config=daemon)
        await manager.create_session(provider="claude", conversation_id="conv-claude")
        with pytest.raises(RuntimeError, match="sensitive"):
            await manager.create_session(provider="codex", conversation_id="conv-codex")


class TestDroidSrtWrap:
    @pytest.mark.asyncio
    async def test_droid_attach_wraps_argv_once_and_merges_env(self, tmp_path: Path) -> None:
        from gobby.servers.websocket.chat.backends.base import ProviderBackendHealth
        from gobby.servers.websocket.chat.backends.droid import (
            DroidManagedChatSession,
            DroidWebChatBackend,
        )

        launch = _srt_launch(executable="/bin/droid")
        process = MagicMock()
        process.stdin = MagicMock()
        process.stdout = MagicMock()
        process.stderr = None
        captured: dict[str, Any] = {}

        async def fake_prepare(**kwargs: Any) -> SandboxLaunch:
            captured["env"] = dict(kwargs["env"])
            captured["workspace_path"] = kwargs["workspace_path"]
            captured["config"] = kwargs["config"]
            return launch

        async def fake_init(handle: Any, session: Any, cwd: str) -> SimpleNamespace:
            return SimpleNamespace(data={"session_id": "droid-1"})

        backend = DroidWebChatBackend(
            sandbox_config=SandboxConfig(
                enabled=True,
                backend="srt",
                allow_network=False,
                allow_git_network=True,
                allow_package_registries=True,
            )
        )
        session = DroidManagedChatSession(conversation_id="conv-droid", _backend=backend)
        session.project_path = str(tmp_path)
        session.db_session_id = "sess-droid"

        with (
            patch(
                "gobby.servers.websocket.chat.backends.droid.shutil.which",
                return_value="/bin/droid",
            ),
            patch(
                "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=process,
            ) as create_process,
            patch(
                "gobby.servers.websocket.chat.backends.droid.prepare_sandbox_launch",
                new=fake_prepare,
                create=True,
            ),
            patch.object(backend, "start", new_callable=AsyncMock),
            patch.object(backend, "_initialize_session", new=fake_init),
            patch.object(backend, "_log_process_stderr", new_callable=AsyncMock),
        ):
            backend._health = ProviderBackendHealth(provider="droid", available=True)
            await backend.attach_session(session, model="gpt-5.4")

        create_process.assert_called_once()
        argv = list(create_process.call_args.args)
        expected = launch.wrap(
            [
                "/bin/droid",
                "exec",
                "--input-format",
                "stream-jsonrpc",
                "--output-format",
                "stream-jsonrpc",
                "--auto",
                "low",
                "--cwd",
                str(tmp_path),
                "--model",
                "gpt-5.4",
            ]
        )
        assert argv[: len(expected)] == expected
        assert argv.count("/usr/bin/node") == 1
        env = create_process.call_args.kwargs["env"]
        assert env["TMPDIR"] == "/tmp/srt-run"
        assert env["GOBBY_WEB_CHAT_CHILD"] == "1"
        assert captured["env"]["GOBBY_WEB_CHAT_CHILD"] == "1"
        assert captured["config"].backend == "srt"
        assert captured["config"].allow_network is False


class TestCodexNativeSandboxPin:
    def test_srt_pin_uses_danger_full_access(self) -> None:
        from gobby.servers.websocket.chat.backends.codex import CodexWebChatBackend

        config = SandboxConfig(enabled=True, backend="srt", allow_network=False)
        pinned = CodexWebChatBackend.native_sandbox_pin(config)
        assert pinned == "danger-full-access"

    def test_provider_native_keeps_thread_policy(self) -> None:
        from gobby.agents.sandbox_resolvers import CodexSandboxResolver
        from gobby.servers.websocket.chat.backends.codex import CodexWebChatBackend

        config = SandboxConfig(enabled=True, backend="provider-native", mode="permissive")
        pinned = CodexWebChatBackend.native_sandbox_pin(config)
        assert pinned == CodexSandboxResolver.thread_sandbox_policy(config)


class TestRuntimeManagerDoesNotWarmStart:
    @pytest.mark.asyncio
    async def test_start_does_not_launch_shared_codex_or_acp(self) -> None:
        manager = WebChatRuntimeManager(daemon_config=DaemonConfig())
        await manager.start()

        assert manager._grok_backend._client.is_started is False
        assert manager._qwen_backend._client.is_started is False
        client = manager._codex_backend.client
        assert client is None or client.is_connected is False
