"""Launch-contract matrix for web-chat SRT wrapping."""

from __future__ import annotations

import importlib
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
from gobby.agents.sandbox_policy import (
    allowed_domains,
    provider_read_exceptions,
    provider_write_exceptions,
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
        assert (
            snapshot.config is manager._sandbox_config or snapshot.config == manager._sandbox_config
        )


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

        backend = DroidWebChatBackend()
        session = DroidManagedChatSession(
            conversation_id="conv-droid",
            _backend=backend,
            sandbox_config=SandboxConfig(
                enabled=True,
                backend="srt",
                allow_network=False,
                allow_git_network=True,
                allow_package_registries=True,
            ),
        )
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


class TestInterleavedSessionCreatesKeepOwnPolicy:
    @pytest.mark.asyncio
    async def test_delayed_first_launch_uses_its_own_sandbox_snapshot(self, tmp_path: Path) -> None:
        from gobby.servers.websocket.chat.backends.base import ProviderBackendHealth
        from gobby.servers.websocket.chat.backends.droid import (
            DroidManagedChatSession,
            DroidWebChatBackend,
        )

        holder: dict[str, DaemonConfig] = {"config": DaemonConfig()}
        manager = WebChatRuntimeManager(
            daemon_config=holder["config"],
            config_resolver=lambda: holder["config"],
        )
        session_a = await manager.create_session(provider="droid", conversation_id="conv-a")
        snapshot_a = session_a.sandbox_config.model_copy(deep=True)
        hash_a = session_a.sandbox_policy_hash

        holder["config"] = DaemonConfig(
            web_chat_sandbox={
                "backend": "srt",
                "allow_network": False,
                "allowed_domains": ["x.test"],
            }
        )
        session_b = await manager.create_session(provider="droid", conversation_id="conv-b")

        assert session_b.sandbox_policy_hash != hash_a
        assert session_a.sandbox_policy_hash == hash_a
        assert session_a.sandbox_config.allowed_domains == snapshot_a.allowed_domains

        captured: dict[str, Any] = {}

        async def fake_prepare(**kwargs: Any) -> SandboxLaunch:
            captured["config"] = kwargs["config"]
            return _srt_launch(executable="/bin/droid")

        process = MagicMock()
        process.stdin = MagicMock()
        process.stdout = MagicMock()
        process.stderr = None
        session_a.project_path = str(tmp_path)
        session_a.db_session_id = "sess-a"
        backend = manager._droid_backend
        assert isinstance(backend, DroidWebChatBackend)

        async def fake_init(_handle: Any, _session: Any, _cwd: str) -> SimpleNamespace:
            return SimpleNamespace(data={"session_id": "droid-a"})

        with (
            patch(
                "gobby.servers.websocket.chat.backends.droid.shutil.which",
                return_value="/bin/droid",
            ),
            patch(
                "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=process,
            ),
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
            assert isinstance(session_a, DroidManagedChatSession)
            await backend.attach_session(session_a, model="gpt-5.4")

        assert captured["config"].allowed_domains == snapshot_a.allowed_domains
        assert captured["config"].backend == snapshot_a.backend


class TestAgySrtWrap:
    def test_agy_stale_hash_refuses_resume(self) -> None:
        manager = WebChatRuntimeManager(daemon_config=DaemonConfig())
        session = SimpleNamespace(
            session_type="web_chat",
            source="agy",
            sandbox_policy_hash="stale-hash",
        )

        reason = manager.policy_mismatch_reason(session)

        assert reason == web_chat_policy_mismatch_message()

    @pytest.mark.asyncio
    async def test_agy_attach_wraps_argv_once_and_merges_identity_env(self, tmp_path: Path) -> None:
        spec = importlib.util.find_spec("gobby.servers.websocket.chat.backends.agy")
        assert spec is not None
        from gobby.servers.websocket.chat.backends.agy import (
            AgyManagedChatSession,
            AgyWebChatBackend,
        )

        launch = _srt_launch(executable="/bin/agy")
        process = MagicMock()
        process.stdin = MagicMock()
        process.stdout = MagicMock()
        process.stderr = None
        captured: dict[str, Any] = {}

        async def fake_prepare(**kwargs: Any) -> SandboxLaunch:
            captured["env"] = dict(kwargs["env"])
            captured["workspace_path"] = kwargs["workspace_path"]
            captured["config"] = kwargs["config"]
            captured["provider"] = kwargs["provider"]
            return launch

        backend = AgyWebChatBackend()
        session = AgyManagedChatSession(
            conversation_id="conv-agy",
            _backend=backend,
            sandbox_config=SandboxConfig(
                enabled=True,
                backend="srt",
                allow_network=False,
                allow_git_network=True,
                allow_package_registries=True,
            ),
        )
        session.project_path = str(tmp_path)
        session.db_session_id = "sess-agy"
        session.project_id = "proj-agy"

        with (
            patch(
                "gobby.servers.websocket.chat.backends.agy.shutil.which",
                return_value="/bin/agy",
            ),
            patch(
                "gobby.servers.websocket.chat.backends.agy.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=process,
            ) as create_process,
            patch(
                "gobby.servers.websocket.chat.backends.agy.prepare_sandbox_launch",
                new=fake_prepare,
                create=True,
            ),
            patch.object(backend, "start", new_callable=AsyncMock),
            patch.object(backend, "_log_process_stderr", new_callable=AsyncMock),
        ):
            from gobby.servers.websocket.chat.backends.base import ProviderBackendHealth

            backend._health = ProviderBackendHealth(provider="agy", available=True)
            await backend.attach_session(session, model="gemini-3-flash")

        create_process.assert_called_once()
        argv = list(create_process.call_args.args)
        inner = [
            "/bin/agy",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
            "--dangerously-skip-permissions",
            "--add-dir",
            str(tmp_path),
            "--print-timeout",
            "2562047h",
            "--sandbox=false",
            "--model",
            "gemini-3-flash",
        ]
        expected = launch.wrap(inner)
        assert argv[: len(expected)] == expected
        assert argv.count("/usr/bin/node") == 1
        assert "--sandbox=false" in argv
        env = create_process.call_args.kwargs["env"]
        assert env["TMPDIR"] == "/tmp/srt-run"
        assert env["GOBBY_WEB_CHAT_CHILD"] == "1"
        assert env["GOBBY_SESSION_ID"] == "sess-agy"
        assert env["GOBBY_PROJECT_ID"] == "proj-agy"
        assert "GOBBY_HOOKS_DISABLED" not in env
        assert captured["env"]["GOBBY_SESSION_ID"] == "sess-agy"
        assert captured["provider"] == "agy"
        assert captured["config"].backend == "srt"
        assert captured["config"].allow_network is False
        reads = provider_read_exceptions("agy", captured["env"])
        writes = provider_write_exceptions("agy")
        domains = allowed_domains(captured["config"], "agy", None)
        assert any("antigravity-cli" in path for path in writes)
        assert any("keychain" in path.lower() or "Keychains" in path for path in reads)
        assert "daily-cloudcode-pa.googleapis.com" in domains


# --- Plan row 3.1.7: ACP subprocesses are session-owned at the post-hydration seam ---


class _SeamACPClient:
    """Operation-owned ACP client fake that records its launch inputs."""

    def __init__(
        self,
        registry: list[_SeamACPClient],
        *,
        cwd: str | None = None,
        sandbox_config: Any = None,
        sandbox_run_id: str | None = None,
        fail_start: bool = False,
        **_: Any,
    ) -> None:
        self.cwd = cwd
        self.sandbox_config = sandbox_config
        self.sandbox_run_id = sandbox_run_id
        self.fail_start = fail_start
        self.is_started = False
        self.session_id: str | None = None
        self.session_capabilities: dict[str, bool] = {"resume": True}
        self.agent_capabilities: dict[str, Any] = {}
        self.stopped = False
        self.resumed: str | None = None
        self.created = False
        self.start_kwargs: dict[str, Any] = {}
        registry.append(self)

    async def start(self, **kwargs: Any) -> None:
        if self.fail_start:
            raise RuntimeError("acp launch failed")
        self.is_started = True
        self.start_kwargs = kwargs

    async def stop(self) -> None:
        self.stopped = True
        self.is_started = False

    async def create_session(self, **_: Any) -> dict[str, Any]:
        self.created = True
        self.session_id = f"acp-new-{len(self.start_kwargs)}"
        return {"sessionId": self.session_id}

    async def resume_session(self, session_id: str, **_: Any) -> dict[str, Any]:
        self.resumed = session_id
        self.session_id = session_id
        return {"sessionId": session_id}


def _seam_owner() -> SimpleNamespace:
    return SimpleNamespace(
        _chat_sessions={},
        clients={},
        _fire_lifecycle=AsyncMock(return_value=None),
        web_chat_session_registry=None,
    )


def _seam_context(workspace: Path, *domains: str) -> Any:
    from gobby.servers.websocket.chat._session_launch import SessionLaunchContext

    config = SandboxConfig(
        enabled=True,
        backend="srt",
        allow_network=False,
        allowed_domains=list(domains),
    )
    return SessionLaunchContext(
        sandbox=SandboxPolicySnapshot(config=config, policy_hash=f"hash-{'-'.join(domains)}"),
        workspace_path=str(workspace),
    )


async def _seam_start(owner: Any, session: Any, context: Any) -> Any:
    from gobby.servers.websocket.chat._session_launch import start_hydrated_session

    return await start_hydrated_session(
        owner,
        session,
        context,
        session_key=session.conversation_id,
        effective_model="qwen3-coder",
        persona_selected=False,
        pending_agent=None,
        pending_mode="plan",
        agent_name="qwen",
        provider_name="qwen",
        session_manager=None,
        existing_db_session=None,
        project_context_changed=False,
        effective_pid="proj",
    )


class TestAcpSubprocessesAreSessionOwned:
    @pytest.fixture
    def acp(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from gobby.servers.websocket.chat.backends.base import ProviderBackendHealth

        manager = WebChatRuntimeManager(daemon_config=DaemonConfig())
        backend = manager._qwen_backend
        clients: list[_SeamACPClient] = []
        failures: list[bool] = []

        def factory(**kwargs: Any) -> _SeamACPClient:
            fail = failures.pop(0) if failures else False
            return _SeamACPClient(clients, fail_start=fail, **kwargs)

        monkeypatch.setattr(backend, "acp_client_cls", factory)
        backend._health = ProviderBackendHealth(provider="qwen", available=True)
        return SimpleNamespace(manager=manager, backend=backend, clients=clients, failures=failures)

    @pytest.fixture(autouse=True)
    def _no_trust_writes(self) -> Any:
        with (
            patch("gobby.servers.websocket.chat.backends.acp.pre_approve_directory"),
            patch(
                "gobby.servers.websocket.chat.backends.acp.ACPWebChatBackend.start",
                new_callable=AsyncMock,
            ),
        ):
            yield

    @pytest.mark.asyncio
    async def test_first_start_launches_under_final_path_and_policy(
        self, acp: Any, tmp_path: Path
    ) -> None:
        session = await acp.manager.create_session(
            provider="qwen", conversation_id="conv-first", model="qwen3-coder"
        )
        assert acp.clients == []
        owner = _seam_owner()
        workspace = tmp_path / "wt-a"
        workspace.mkdir()

        await _seam_start(owner, session, _seam_context(workspace, "a.test"))

        assert owner._chat_sessions["conv-first"] is session
        assert len(acp.clients) == 1
        client = acp.clients[0]
        assert client.cwd == str(workspace.resolve())
        assert client.start_kwargs["cwd"] == str(workspace.resolve())
        assert client.sandbox_config.allowed_domains == ["a.test"]
        assert client.created is True
        assert session.is_connected is True
        assert session.project_path == str(workspace)

    @pytest.mark.asyncio
    async def test_resume_reuses_persisted_identity(self, acp: Any, tmp_path: Path) -> None:
        session = await acp.manager.create_session(
            provider="qwen", conversation_id="conv-resume", model="qwen3-coder"
        )
        session.resume_session_id = "acp-old"

        await _seam_start(_seam_owner(), session, _seam_context(tmp_path, "a.test"))

        assert acp.clients[0].resumed == "acp-old"
        assert acp.clients[0].created is False
        assert session.sdk_session_id == "acp-old"

    @pytest.mark.asyncio
    async def test_failed_resume_falls_back_to_fresh_launch(self, acp: Any, tmp_path: Path) -> None:
        session = await acp.manager.create_session(
            provider="qwen", conversation_id="conv-fallback", model="qwen3-coder"
        )
        session.resume_session_id = "acp-old"
        acp.failures.extend([True, False])

        await _seam_start(_seam_owner(), session, _seam_context(tmp_path, "a.test"))

        assert len(acp.clients) == 2
        assert acp.clients[0].stopped is True
        assert acp.clients[1].resumed is None
        assert acp.clients[1].created is True
        assert session.resume_session_id is None
        assert session.is_connected is True

    @pytest.mark.asyncio
    async def test_failed_start_cleans_up_and_registers_nothing(
        self, acp: Any, tmp_path: Path
    ) -> None:
        session = await acp.manager.create_session(
            provider="qwen", conversation_id="conv-fail", model="qwen3-coder"
        )
        acp.failures.append(True)
        owner = _seam_owner()

        with pytest.raises(RuntimeError, match="acp launch failed"):
            await _seam_start(owner, session, _seam_context(tmp_path, "a.test"))

        assert owner._chat_sessions == {}
        assert all(client.stopped for client in acp.clients)
        assert session.is_connected is False
        assert session._acp_client is None
        owner._fire_lifecycle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_teardown_stops_the_session_owned_client(self, acp: Any, tmp_path: Path) -> None:
        session = await acp.manager.create_session(
            provider="qwen", conversation_id="conv-teardown", model="qwen3-coder"
        )
        await _seam_start(_seam_owner(), session, _seam_context(tmp_path, "a.test"))
        client = acp.clients[0]
        assert client.stopped is False

        await session.stop()

        assert client.stopped is True
        assert session.is_connected is False
        assert session._acp_client is None

    @pytest.mark.asyncio
    async def test_concurrent_sessions_get_distinct_confinement_roots(
        self, acp: Any, tmp_path: Path
    ) -> None:
        import asyncio

        session_a = await acp.manager.create_session(
            provider="qwen", conversation_id="conv-a", model="qwen3-coder"
        )
        session_b = await acp.manager.create_session(
            provider="qwen", conversation_id="conv-b", model="qwen3-coder"
        )
        worktree_a = tmp_path / "project-a" / "wt"
        worktree_b = tmp_path / "project-b" / "wt"
        worktree_a.mkdir(parents=True)
        worktree_b.mkdir(parents=True)
        owner = _seam_owner()

        await asyncio.gather(
            _seam_start(owner, session_a, _seam_context(worktree_a, "a.test")),
            _seam_start(owner, session_b, _seam_context(worktree_b, "b.test")),
        )

        assert set(owner._chat_sessions) == {"conv-a", "conv-b"}
        by_run = {client.sandbox_run_id: client for client in acp.clients}
        assert set(by_run) == {"conv-a", "conv-b"}
        assert by_run["conv-a"].cwd == str(worktree_a.resolve())
        assert by_run["conv-b"].cwd == str(worktree_b.resolve())
        assert by_run["conv-a"].cwd != by_run["conv-b"].cwd
        assert by_run["conv-a"].sandbox_config.allowed_domains == ["a.test"]
        assert by_run["conv-b"].sandbox_config.allowed_domains == ["b.test"]
        assert session_a._acp_client is by_run["conv-a"]
        assert session_b._acp_client is by_run["conv-b"]


# --- Plan row 3.1.8: the Claude shim execs the SRT-wrapped argv exactly once ---


def _claude_srt_launch() -> SandboxLaunch:
    return SandboxLaunch(
        backend="srt",
        enforced=True,
        node_path="/bin/echo",
        runner_path="/opt/srt/runner.js",
        policy_path="/tmp/srt-policy.json",
        violation_path="/tmp/srt-violations.jsonl",
        provider_executable="/usr/bin/claude",
        provider_env={"TMPDIR": "/tmp/srt-run"},
        policy_hash="srt-hash",
    )


async def _start_claude_under_srt(
    session: Any,
    tmp_path: Path,
    *,
    connect_error: Exception | None = None,
    disconnect_error: Exception | None = None,
) -> tuple[dict[str, Any], SandboxLaunch]:
    launch = _claude_srt_launch()
    captured: dict[str, Any] = {}
    headless = tmp_path / "headless.json"
    headless.write_text('{"hooks":{"SessionStart":[]}}', encoding="utf-8")

    def capture_options(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return MagicMock()

    async def fake_prepare(**_: Any) -> SandboxLaunch:
        return launch

    with (
        patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
        patch(
            "gobby.servers.chat_session._build_gobby_mcp_entry",
            return_value={"command": "gobby", "args": ["mcp-server"]},
        ),
        patch("gobby.servers.chat_session._find_project_root", return_value=None),
        patch("gobby.servers.chat_session._HEADLESS_SETTINGS", headless),
        patch("gobby.agents.srt_runtime.prepare_sandbox_launch", new=fake_prepare),
        patch("gobby.paths.get_gobby_home", return_value=tmp_path / "home"),
        patch("gobby.servers.chat_session.ClaudeAgentOptions", side_effect=capture_options),
        patch("gobby.servers.chat_session.ClaudeSDKClient") as client_cls,
    ):
        client = AsyncMock()
        if connect_error is not None:
            client.connect.side_effect = connect_error
        if disconnect_error is not None:
            client.disconnect.side_effect = disconnect_error
        client_cls.return_value = client
        await session.start()
    return captured, launch


class TestClaudeShimContract:
    @pytest.fixture
    def session(self) -> Any:
        from gobby.servers.chat_session import ChatSession

        session = ChatSession(conversation_id="conv-claude-shim")
        session.sandbox_config = SandboxConfig(enabled=True, backend="srt")
        session.db_session_id = "db-claude"
        return session

    @pytest.mark.asyncio
    async def test_shim_execs_wrapped_argv_and_passes_sdk_args_once(
        self, session: Any, tmp_path: Path
    ) -> None:
        import subprocess

        captured, launch = await _start_claude_under_srt(session, tmp_path)

        shim = Path(captured["cli_path"])
        assert shim.parent == tmp_path / "home" / "run" / "shims"
        assert shim.is_file()
        assert shim.stat().st_mode & 0o777 == 0o700
        script = shim.read_text(encoding="utf-8")
        assert script.startswith("#!/bin/sh\nexec ")
        assert script.count('"$@"') == 1
        wrapped = launch.wrap(["/usr/bin/claude"])
        run = subprocess.run(
            [str(shim), "--sdk-appended", "x y"], capture_output=True, text=True, check=True
        )
        argv = run.stdout.split()
        assert argv == [*wrapped[1:], "--sdk-appended", "x", "y"]
        assert argv.count("/opt/srt/runner.js") == 1
        assert argv.count("--") == 1
        assert captured["env"]["TMPDIR"] == "/tmp/srt-run"
        assert captured["env"]["GOBBY_SESSION_ID"] == "db-claude"
        session._cleanup_sandbox_launch()

    @pytest.mark.asyncio
    async def test_shim_is_removed_on_teardown(self, session: Any, tmp_path: Path) -> None:
        captured, _ = await _start_claude_under_srt(session, tmp_path)
        shim = Path(captured["cli_path"])
        assert shim.exists()

        await session.stop()

        assert not shim.exists()
        assert session._sandbox_launch is None
        assert session.is_connected is False

    @pytest.mark.asyncio
    async def test_shim_is_removed_on_disconnect_failure(
        self, session: Any, tmp_path: Path
    ) -> None:
        captured, _ = await _start_claude_under_srt(
            session,
            tmp_path,
            disconnect_error=RuntimeError("Attempted to exit cancel scope in a different task"),
        )
        shim = Path(captured["cli_path"])

        await session.stop()

        assert not shim.exists()
        assert session._sandbox_launch is None
        assert session._client is None

    @pytest.mark.asyncio
    async def test_shim_is_removed_on_failed_start(self, session: Any, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="connect failed"):
            await _start_claude_under_srt(
                session, tmp_path, connect_error=RuntimeError("connect failed")
            )

        shims = list((tmp_path / "home" / "run" / "shims").glob("gobby-srt-shim-*"))
        assert shims == []
        assert session._sandbox_launch is None
        assert session.is_connected is False
