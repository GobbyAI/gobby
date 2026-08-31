"""Tests for shared web-chat runtime manager and provider backends."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.adapters import acp_client_requests
from gobby.adapters.acp_client import StreamEvent
from gobby.agents.local_model import LocalModelError
from gobby.agents.sandbox import SandboxConfig
from gobby.config.ai import GenerationEndpointConfig
from gobby.config.app import DaemonConfig
from gobby.llm.claude_models import DoneEvent, TextChunk, ToolCallEvent, ToolResultEvent
from gobby.providers.version_gate import AGY_UNPUBLISHED_REASON
from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat.backends import (
    CodexManagedChatSession,
    CodexWebChatBackend,
    DroidManagedChatSession,
    GrokManagedChatSession,
    GrokWebChatBackend,
    QwenManagedChatSession,
    QwenWebChatBackend,
)
from gobby.servers.websocket.chat.backends.agy import AgyManagedChatSession
from gobby.servers.websocket.chat.backends.base import ProviderBackendHealth
from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.skills.formatting import skill_fetch_directive

pytestmark = pytest.mark.unit

PYTHON_SKILL_DIRECTIVE = skill_fetch_directive("python")
CODE_INDEX_SKILL_DIRECTIVE = skill_fetch_directive("code-index")
TASKS_SKILL_DIRECTIVE = skill_fetch_directive("tasks")


def _async_stream(*items: Any):
    async def _gen():
        for item in items:
            yield item

    return _gen()


class TestWebChatRuntimeManager:
    async def test_sandbox_policy_reads_live_config_for_new_sessions(self) -> None:
        current = [DaemonConfig(web_chat_sandbox={"enabled": False})]
        manager = WebChatRuntimeManager(
            codex_client=None,
            daemon_config=current[0],
            config_resolver=lambda: current[0],
        )
        initial_hash = manager.sandbox_policy_hash

        current[0] = DaemonConfig(
            web_chat_sandbox={"enabled": True, "extra_read_paths": ["/tmp/live"]}
        )
        session = await manager.create_session(provider="claude", conversation_id="live-config")

        assert manager.sandbox_config.enabled is True
        assert manager.sandbox_config.extra_read_paths == ["/tmp/live"]
        assert manager.sandbox_policy_hash != initial_hash

        assert isinstance(session, ChatSession)
        assert session.sandbox_config is not None
        assert session.sandbox_config.enabled is True
        assert session.sandbox_config.extra_read_paths == ["/tmp/live"]

    @pytest.mark.asyncio
    async def test_live_sandbox_refresh_never_touches_backend_state(self) -> None:
        current = [DaemonConfig(web_chat_sandbox={"enabled": False})]
        manager = WebChatRuntimeManager(
            codex_client=None,
            daemon_config=current[0],
            config_resolver=lambda: current[0],
        )
        backends = (
            manager._claude_backend,
            manager._codex_backend,
            manager._grok_backend,
            manager._qwen_backend,
            manager._droid_backend,
            manager._agy_backend,
        )
        for backend in backends:
            assert not hasattr(backend, "set_sandbox_config")
            assert not hasattr(backend, "_sandbox_config")

        current[0] = DaemonConfig(web_chat_sandbox={"enabled": True})
        snapshot = manager._refresh_sandbox_config()
        assert snapshot.config.enabled is True
        assert manager.sandbox_config.enabled is True
        for backend in backends:
            assert not hasattr(backend, "_sandbox_config")

        session = await manager.create_session(provider="droid", conversation_id="conv-live")
        assert isinstance(session, DroidManagedChatSession)
        assert session.sandbox_config is not None
        assert session.sandbox_config.enabled is True
        assert session.sandbox_config is not snapshot.config
        assert session.sandbox_policy_hash == snapshot.policy_hash

    async def test_create_session_routes_by_provider(self) -> None:
        manager = WebChatRuntimeManager(
            codex_client=None,
            daemon_config=DaemonConfig(web_chat_sandbox={"enabled": False}),
        )

        record = SimpleNamespace(supported=True, reason="AGY 1.1.18 meets required version 1.1.18.")
        with patch(
            "gobby.providers.version_gate.ensure_agy_support",
            AsyncMock(return_value=record),
        ):
            claude_session = await manager.create_session(
                provider="claude", conversation_id="conv-1"
            )
            grok_session = await manager.create_session(provider="grok", conversation_id="conv-2")
            qwen_session = await manager.create_session(provider="qwen", conversation_id="conv-3")
            codex_session = await manager.create_session(provider="codex", conversation_id="conv-4")
            agy_error: Exception | None = None
            try:
                agy_session = await manager.create_session(
                    provider="agy", conversation_id="conv-agy"
                )
            except Exception as exc:
                agy_error = exc
                agy_session = None

        assert isinstance(claude_session, ChatSession)
        assert isinstance(grok_session, GrokManagedChatSession)
        assert isinstance(qwen_session, QwenManagedChatSession)
        assert isinstance(codex_session, CodexManagedChatSession)
        assert agy_error is None
        assert isinstance(agy_session, AgyManagedChatSession)

    async def test_create_session_rejects_unsupported_provider(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)

        with pytest.raises(RuntimeError, match="Unsupported web chat provider: unknown"):
            await manager.create_session(provider="unknown", conversation_id="conv-unknown")
        with pytest.raises(RuntimeError, match="Unsupported web chat provider: unsupported"):
            await manager.create_session(provider="unsupported", conversation_id="conv-unsupported")

    def test_health_snapshot_contains_droid(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)

        health = manager.health_snapshot()

        assert "droid" in health
        assert health["droid"]["provider"] == "droid"

    def test_health_reports_removed_local_selector_instead_of_raising(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)

        health = manager.health("local:studio")

        assert health.provider == "local:studio"
        assert health.available is False
        assert health.startup_error is not None
        assert "removed local: selector" in health.startup_error

    def test_acp_backends_expose_grok_and_qwen_only(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)

        backends = manager.acp_backends()

        assert set(backends) == {"grok", "qwen"}
        assert isinstance(backends["grok"], GrokWebChatBackend)
        assert isinstance(backends["qwen"], QwenWebChatBackend)
        assert manager.acp_backend("grok") is backends["grok"]
        assert manager.acp_backend("qwen") is backends["qwen"]
        assert manager.acp_backend("codex") is None

    def test_acp_session_capabilities_default_empty_before_initialize(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)

        # No initialize handshake yet → graceful-degradation default.
        assert manager.acp_session_capabilities("grok") == {}
        assert manager.acp_session_capabilities("codex") == {}

    def test_acp_session_info_cache_roundtrip_is_isolated(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)

        assert manager.get_acp_session_info("grok", "s1") is None

        manager.cache_acp_session_info("grok", "s1", {"sessionId": "s1", "cwd": "/repo"})
        cached = manager.get_acp_session_info("grok", "s1")
        assert cached == {"sessionId": "s1", "cwd": "/repo"}

        # Returned copies are isolated from the internal cache.
        cached["cwd"] = "/mutated"
        assert manager.get_acp_session_info("grok", "s1") == {"sessionId": "s1", "cwd": "/repo"}
        assert manager.acp_session_infos() == {("grok", "s1"): {"sessionId": "s1", "cwd": "/repo"}}

    async def test_create_session_uses_srt_for_droid(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)

        session = await manager.create_session(provider="droid", conversation_id="conv-droid")

        assert isinstance(session, DroidManagedChatSession)
        assert manager.sandbox_config.backend == "srt"

    def test_health_uses_shared_agy_unavailable_reason(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)

        health = manager.health("agy")

        assert health.available is False
        assert health.startup_error == AGY_UNPUBLISHED_REASON

    def test_health_agy_supported_uses_backend_without_reprobe(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)
        record = SimpleNamespace(
            supported=True,
            reason="AGY 1.1.18 meets required version 1.1.18.",
        )
        assert hasattr(manager, "_agy_backend")
        manager._agy_backend._health = ProviderBackendHealth(provider="agy", available=True)
        probe = MagicMock()

        with (
            patch("gobby.providers.version_gate.peek_agy_support", return_value=record),
            patch("gobby.providers.version_gate.probe_and_publish_agy_support", probe),
            patch("gobby.providers.version_gate.ensure_agy_support", probe),
        ):
            health = manager.health("agy")

        assert health.available is True
        assert health.provider == "agy"
        probe.assert_not_called()

    async def test_create_session_returns_agy_session_when_supported(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)
        record = SimpleNamespace(
            supported=True,
            reason="AGY 1.1.18 meets required version 1.1.18.",
        )

        error: Exception | None = None
        session = None
        with patch(
            "gobby.providers.version_gate.ensure_agy_support",
            AsyncMock(return_value=record),
        ):
            try:
                session = await manager.create_session(provider="agy", conversation_id="conv-agy")
            except Exception as exc:
                error = exc

        assert error is None
        assert isinstance(session, AgyManagedChatSession)
        assert session.provider == "agy"
        assert session.conversation_id == "conv-agy"
        assert session._backend is manager._agy_backend

    async def test_create_session_rejects_unsupported_agy_record(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)
        record = SimpleNamespace(supported=False, reason="agy CLI not found")

        with (
            patch(
                "gobby.providers.version_gate.ensure_agy_support",
                AsyncMock(return_value=record),
            ),
            pytest.raises(RuntimeError, match="agy CLI not found"),
        ):
            await manager.create_session(provider="agy", conversation_id="conv-agy")

    async def test_create_session_routes_codex_local_selector_to_oss_backend(self) -> None:
        config = DaemonConfig(
            web_chat_sandbox={"enabled": False},
            ai={
                "generation": {
                    "endpoints": {
                        "ollama": {
                            "protocol": "ollama",
                            "api_base": "http://localhost:11434",
                            "model": "llama3.2:latest",
                        }
                    }
                }
            },
        )
        manager = WebChatRuntimeManager(codex_client=MagicMock(), daemon_config=config)

        session = await manager.create_session(
            provider="codex",
            conversation_id="conv-codex-local",
            model="endpoint:ollama/ollama/qwen3-coder",
        )

        assert isinstance(session, CodexManagedChatSession)
        assert session._model == "ollama/qwen3-coder"
        assert session.model == "endpoint:ollama/ollama/qwen3-coder"
        local_backend = manager._codex_endpoint_backends["ollama"]
        assert session._backend is local_backend
        assert local_backend.client is not None
        assert local_backend.client._global_args == (
            "--oss",
            "--local-provider",
            "ollama",
        )

    def test_runtime_manager_skips_invalid_responses_endpoint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = DaemonConfig(
            web_chat_sandbox={"enabled": False},
            ai={
                "generation": {
                    "endpoints": {
                        "openrouter": {
                            "wire_api": "responses",
                            "api_base": "https://openrouter.ai/api/v1",
                            "api_key": "test-openrouter-key",
                            "model": "moonshotai/kimi-k3",
                        }
                    }
                }
            },
        )
        monkeypatch.setattr(
            "gobby.servers.websocket.chat.runtime_manager.codex_endpoint_config_overrides",
            MagicMock(side_effect=ValueError("invalid endpoint")),
        )

        manager = WebChatRuntimeManager(daemon_config=config)

        assert manager._codex_endpoint_backends == {}

    async def test_codex_endpoint_selector_uses_canonical_wire_model(self) -> None:
        config = DaemonConfig(
            web_chat_sandbox={"enabled": False},
            ai={
                "generation": {
                    "endpoints": {
                        "openrouter": {
                            "protocol": "openai-compatible",
                            "api_base": "https://openrouter.ai/api/v1",
                            "api_key": "test-openrouter-key",
                            "model": "moonshotai/kimi-k3",
                            "wire_api": "responses",
                        }
                    }
                }
            },
        )
        manager = WebChatRuntimeManager(codex_client=MagicMock(), daemon_config=config)
        selector = "endpoint:openrouter/moonshotai/kimi-k3"
        session = await manager.create_session(
            provider="codex",
            conversation_id="conv-codex-responses",
            model=selector,
        )
        assert isinstance(session, CodexManagedChatSession)

        backend = manager._codex_endpoint_backends["openrouter"]
        backend.start = AsyncMock()
        backend._health.available = True
        client = backend.client
        assert client is not None
        client.start_thread = AsyncMock(
            return_value=SimpleNamespace(id="thread-responses", path=None)
        )

        await session.start(model=selector)

        assert session.model == selector
        assert session._model == "moonshotai/kimi-k3"
        assert client.start_thread.await_args is not None
        assert client.start_thread.await_args.kwargs["model"] == "moonshotai/kimi-k3"

    async def test_create_session_applies_codex_transcript_retry_config(self) -> None:
        manager = WebChatRuntimeManager(
            codex_client=None,
            codex_transcript_retry_attempts=2,
            codex_transcript_retry_delay_seconds=0.25,
            daemon_config=DaemonConfig(web_chat_sandbox={"enabled": False}),
        )

        codex_session = await manager.create_session(provider="codex", conversation_id="conv-3")

        assert isinstance(codex_session, CodexManagedChatSession)
        assert codex_session._transcript_retry_attempts == 2
        assert codex_session._transcript_retry_delay_seconds == 0.25

    def test_manager_uses_daemon_owned_web_chat_sandbox_defaults(self) -> None:
        manager = WebChatRuntimeManager(
            codex_client=None,
            daemon_config=DaemonConfig(
                web_chat_sandbox={
                    "enabled": False,
                    "mode": "restrictive",
                    "allow_network": False,
                    "extra_read_paths": ["/tmp/web-read"],
                    "extra_write_paths": ["/tmp/web-write"],
                },
            ),
        )

        snapshot = manager._refresh_sandbox_config()
        assert snapshot.config.enabled is False
        assert snapshot.config.mode == "restrictive"
        assert snapshot.config.allow_network is False
        assert snapshot.config.extra_read_paths == ["/tmp/web-read"]
        assert snapshot.config.extra_write_paths == ["/tmp/web-write"]
        assert manager.sandbox_config == snapshot.config
        for backend in (
            manager._claude_backend,
            manager._codex_backend,
            manager._grok_backend,
            manager._qwen_backend,
            manager._droid_backend,
            manager._agy_backend,
        ):
            assert not hasattr(backend, "_sandbox_config")

    def test_manager_defaults_web_chat_sandbox_to_enabled(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None, daemon_config=DaemonConfig())

        assert manager.sandbox_config.enabled is True
        assert manager.sandbox_policy_hash

    def test_manager_handles_daemon_config_without_embeddings(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None, daemon_config=SimpleNamespace())

        assert manager._qwen_backend._local_generation_endpoints == {}

    @pytest.mark.asyncio
    async def test_background_start_skips_acp_backends(self) -> None:
        manager = WebChatRuntimeManager(codex_client=None)
        manager._codex_backend.start = AsyncMock()
        manager._grok_backend.start = AsyncMock()
        manager._qwen_backend.start = AsyncMock()
        manager._droid_backend.start = AsyncMock()
        assert hasattr(manager, "_agy_backend")
        manager._agy_backend.start = AsyncMock()

        result = await manager.start(background=True)

        assert result is None
        assert manager.sandbox_config.enabled is True
        manager._codex_backend.start.assert_awaited_once_with(background=True)
        manager._droid_backend.start.assert_awaited_once_with(background=True)
        manager._agy_backend.start.assert_awaited_once_with(background=True)
        manager._grok_backend.start.assert_not_awaited()
        manager._qwen_backend.start.assert_not_awaited()

    def test_start_and_stop_include_agy_backend(self) -> None:
        import inspect

        start_src = inspect.getsource(WebChatRuntimeManager.start)
        stop_src = inspect.getsource(WebChatRuntimeManager.stop)
        assert "self._agy_backend.start" in start_src
        assert "self._agy_backend.start(background=True)" in start_src
        assert "self._agy_backend.stop" in stop_src


class TestGrokBackend:
    def test_backend_does_not_build_full_process_sandboxed_acp_client(self) -> None:
        with patch.object(GrokWebChatBackend, "acp_client_cls") as mock_client:
            GrokWebChatBackend()

        # Provider/display_name now come from class attributes on the ACP client;
        # the backend should not pass any sandbox-leaking process args.
        assert mock_client.call_args is not None
        kwargs = mock_client.call_args.kwargs
        assert "extra_args" not in kwargs
        assert "env_overrides" not in kwargs

    @pytest.mark.asyncio
    async def test_start_marks_backend_unavailable_on_error(self) -> None:
        with patch(
            "gobby.servers.websocket.chat.backends.acp.shutil.which",
            return_value=None,
        ):
            backend = GrokWebChatBackend()
            await backend.start()

        health = backend.health()
        assert health.available is False
        assert health.startup_error is not None
        assert "not found" in health.startup_error

    @pytest.mark.asyncio
    async def test_start_reports_cli_available_without_launching(self) -> None:
        with patch(
            "gobby.servers.websocket.chat.backends.acp.shutil.which",
            return_value="/usr/bin/grok",
        ):
            backend = GrokWebChatBackend()
            await backend.start()

        health = backend.health()
        assert health.available is True
        assert health.startup_error is None

    @pytest.mark.asyncio
    async def test_managed_session_translates_stream_events(self) -> None:
        backend = MagicMock()
        backend.attach_session = AsyncMock()
        backend.send_message = MagicMock(
            return_value=_async_stream(
                StreamEvent(event_type="content_delta", data={"content": "Hello "}),
                StreamEvent(event_type="content_delta", data={"content": "Grok"}),
                StreamEvent(event_type="result", data={}),
            )
        )
        session = GrokManagedChatSession(conversation_id="conv-grok", _backend=backend)
        session._connected = True
        session._model = "grok-ctx"
        session._context_window_overrides = {"grok-ctx": 123_000}
        session.sdk_session_id = "sess-1"

        events = [event async for event in session.send_message("hi")]

        assert [e.content for e in events if isinstance(e, TextChunk)] == ["Hello ", "Grok"]
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].context_window == 123_000

    @pytest.mark.asyncio
    async def test_managed_session_defers_tool_lifecycle_context_to_next_turn(self) -> None:
        backend = MagicMock()
        backend.attach_session = AsyncMock()
        backend.send_message = MagicMock(
            side_effect=[
                _async_stream(
                    StreamEvent(
                        event_type="tool_call",
                        data={
                            "tool_name": "Write",
                            "tool_input": {"file_path": "/tmp/example.py"},
                            "call_id": "call-1",
                        },
                    ),
                    StreamEvent(
                        event_type="tool_result",
                        data={"call_id": "call-1", "success": True, "result": "ok"},
                    ),
                    StreamEvent(event_type="result", data={}),
                ),
                _async_stream(StreamEvent(event_type="result", data={})),
            ]
        )
        session = GrokManagedChatSession(conversation_id="conv-grok", _backend=backend)
        session._connected = True
        session.sdk_session_id = "sess-1"
        session._on_pre_tool = AsyncMock(return_value={"context": PYTHON_SKILL_DIRECTIVE})
        session._on_post_tool = AsyncMock(return_value={"context": TASKS_SKILL_DIRECTIVE})

        first_events = [event async for event in session.send_message("first")]
        second_events = [event async for event in session.send_message("second")]

        assert any(isinstance(event, ToolCallEvent) for event in first_events)
        assert any(isinstance(event, ToolResultEvent) for event in first_events)
        session._on_pre_tool.assert_awaited_once_with(
            {"tool_name": "Write", "tool_input": {"file_path": "/tmp/example.py"}}
        )
        session._on_post_tool.assert_awaited_once_with(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/example.py"},
                "tool_response": "ok",
            }
        )
        assert isinstance(second_events[-1], DoneEvent)
        second_prompt = "\n".join(
            block["text"]
            for block in backend.send_message.call_args_list[1].args[1]
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
        assert PYTHON_SKILL_DIRECTIVE in second_prompt
        assert TASKS_SKILL_DIRECTIVE in second_prompt

    @pytest.mark.asyncio
    async def test_managed_session_suppresses_tool_call_when_pre_tool_blocks(self) -> None:
        backend = MagicMock()
        backend.attach_session = AsyncMock()
        backend.send_message = MagicMock(
            return_value=_async_stream(
                StreamEvent(
                    event_type="tool_call",
                    data={
                        "tool_name": "Write",
                        "tool_input": {"file_path": "/tmp/example.py"},
                        "call_id": "call-1",
                    },
                ),
                StreamEvent(
                    event_type="tool_result",
                    data={"call_id": "call-1", "success": True, "result": "ok"},
                ),
                StreamEvent(event_type="result", data={}),
            )
        )
        session = GrokManagedChatSession(conversation_id="conv-grok", _backend=backend)
        session._connected = True
        session.sdk_session_id = "sess-1"
        session._on_pre_tool = AsyncMock(
            return_value={"decision": "block", "context": TASKS_SKILL_DIRECTIVE}
        )
        session._on_post_tool = AsyncMock()

        events = [event async for event in session.send_message("first")]

        assert not any(isinstance(event, ToolCallEvent) for event in events)
        assert not any(isinstance(event, ToolResultEvent) for event in events)
        assert isinstance(events[-1], DoneEvent)
        session._on_pre_tool.assert_awaited_once_with(
            {"tool_name": "Write", "tool_input": {"file_path": "/tmp/example.py"}}
        )
        session._on_post_tool.assert_not_awaited()
        assert session._consume_deferred_context() == TASKS_SKILL_DIRECTIVE

    @pytest.mark.asyncio
    async def test_managed_session_declines_acp_request_permission_when_pre_tool_blocks(
        self,
    ) -> None:
        class RecordingStdin:
            def __init__(self) -> None:
                self.buffer = b""

            def write(self, data: bytes) -> None:
                self.buffer += data

            async def drain(self) -> None:
                return None

        class FakeACPClient:
            cli_name = "grok"
            display_name = "Grok"
            is_started = True

            def __init__(self) -> None:
                self._process = SimpleNamespace(stdin=RecordingStdin())

            async def send(
                self,
                _prompt: str,
                *,
                session_id: str | None = None,
                model: str | None = None,
                reasoning_effort: str | None = None,
                pre_tool_callback: Any = None,
            ):
                del session_id, model, reasoning_effort
                request = {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "sess-1",
                        "options": [
                            {"optionId": "proceed_once", "kind": "allow_once"},
                            {"optionId": "cancel", "kind": "reject_once"},
                        ],
                        "toolCall": {
                            "toolCallId": "tc-1",
                            "title": "list_mcp_servers",
                        },
                    },
                }
                async for event in acp_client_requests.handle_client_request(
                    self,
                    request,
                    pre_tool_callback=pre_tool_callback,
                ):
                    yield event
                yield StreamEvent(event_type="result", data={})

        client = FakeACPClient()
        backend = GrokWebChatBackend(client=client)
        backend._health = ProviderBackendHealth(provider="grok", available=True)
        session = GrokManagedChatSession(conversation_id="conv-grok", _backend=backend)
        session._acp_client = client
        session._connected = True
        session.sdk_session_id = "sess-1"
        session._model = "grok-ctx"
        session._on_pre_tool = AsyncMock(
            return_value={"decision": "block", "context": TASKS_SKILL_DIRECTIVE}
        )

        events = [event async for event in session.send_message("first")]

        assert isinstance(events[-1], DoneEvent)
        session._on_pre_tool.assert_awaited_once_with(
            {
                "tool_name": "list_mcp_servers",
                "tool_input": {"toolCallId": "tc-1", "title": "list_mcp_servers"},
            }
        )
        messages = [
            json.loads(line)
            for line in client._process.stdin.buffer.decode().splitlines()
            if line.strip()
        ]
        assert messages == [
            {
                "jsonrpc": "2.0",
                "id": 99,
                "result": {"outcome": {"outcome": "cancelled"}},
            }
        ]

    def test_plan_mode_context_teaches_gcode(self) -> None:
        session = GrokManagedChatSession(conversation_id="conv-grok", _backend=MagicMock())
        session.chat_mode = "plan"

        context = session._pop_plan_mode_context()

        assert context is not None
        assert "gcode outline/search/symbol" in context
        assert "Bash/exec_command" in context


class TestQwenBackend:
    def test_backend_does_not_build_full_process_sandboxed_acp_client(self) -> None:
        with patch.object(QwenWebChatBackend, "acp_client_cls") as mock_client:
            QwenWebChatBackend()

        # cli_name / display_name / prompt_timeout_env are now class attributes
        # on QwenACPClient; the backend should not pass sandbox-leaking process args.
        assert mock_client.call_args is not None
        kwargs = mock_client.call_args.kwargs
        assert "extra_args" not in kwargs
        assert "env_overrides" not in kwargs

    def test_qwen_inherits_acp_plan_mode_gcode_context(self) -> None:
        session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=MagicMock())
        session.chat_mode = "plan"

        context = session._pop_plan_mode_context()

        assert context is not None
        assert "gcode outline/search/symbol" in context

    @pytest.mark.asyncio
    async def test_interrupt_cancels_pending_acp_tool_approval(self) -> None:
        class FakeBackend:
            def __init__(self) -> None:
                self.cancelled_sessions: list[str | None] = []

            async def interrupt(self, session: QwenManagedChatSession) -> None:
                self.cancelled_sessions.append(session.sdk_session_id)

        backend = FakeBackend()
        session = QwenManagedChatSession(
            conversation_id="qwen-cancel",
            _backend=backend,
            chat_mode="normal",
            project_path=".",
            sdk_session_id="qwen-session",
        )
        approval_ready = asyncio.Event()

        async def mark_approval_ready(
            _tool_use_id: str, _tool_name: str, _input_data: dict[str, Any]
        ) -> None:
            approval_ready.set()

        session._tool_approval_callback = mark_approval_ready
        approval_task = asyncio.create_task(
            session._wait_for_tool_approval("Bash", {"command": "touch blocked"})
        )
        await asyncio.wait_for(approval_ready.wait(), timeout=1.0)

        await session.interrupt()
        result = await approval_task

        assert backend.cancelled_sessions == ["qwen-session"]
        assert acp_client_requests.is_pre_tool_decision_denied(result)

    def test_managed_session_logs_upstream_error_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=MagicMock())
        session.db_session_id = "db-qwen"
        session.sdk_session_id = "sdk-qwen"
        session._model = "qwen3-coder"

        with caplog.at_level("WARNING"):
            event = session._translate_event(
                StreamEvent(
                    event_type="error",
                    data={"message": "Internal error", "code": "upstream_internal"},
                )
            )

        assert isinstance(event, TextChunk)
        assert event.content == "Error: Internal error"
        assert any("Managed qwen upstream error" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_managed_session_done_event_includes_context_window(self) -> None:
        backend = MagicMock()
        backend.attach_session = AsyncMock()
        backend.send_message = MagicMock(
            return_value=_async_stream(StreamEvent(event_type="result", data={}))
        )
        session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=backend)
        session._connected = True
        session.sdk_session_id = "sess-qwen"
        session._model = "qwen3-coder"
        session._context_window_overrides = {"qwen3-coder": 262_144}

        events = [event async for event in session.send_message("hi")]

        assert isinstance(events[-1], DoneEvent)
        assert events[-1].context_window == 262_144

    def test_managed_session_translates_structured_tool_events(self) -> None:
        session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=MagicMock())

        tool_call = session._translate_event(
            StreamEvent(
                event_type="tool_call",
                data={
                    "tool_name": "Write",
                    "tool_input": {"file_path": "/tmp/example.py"},
                    "call_id": "call-1",
                },
            )
        )
        tool_result = session._translate_event(
            StreamEvent(
                event_type="tool_result",
                data={"call_id": "call-1", "success": True, "result": "ok"},
            )
        )

        assert isinstance(tool_call, ToolCallEvent)
        assert tool_call.tool_call_id == "call-1"
        assert tool_call.tool_name == "Write"
        assert tool_call.arguments == {"file_path": "/tmp/example.py"}
        assert isinstance(tool_result, ToolResultEvent)
        assert tool_result.tool_call_id == "call-1"
        assert tool_result.success is True
        assert tool_result.result == "ok"

    @pytest.mark.asyncio
    async def test_attach_session_warms_local_openai_models(self) -> None:
        client = MagicMock()
        client.is_started = True
        client.create_session = AsyncMock(return_value={"sessionId": "sess-qwen"})

        endpoint = GenerationEndpointConfig(
            api_base="http://localhost:1234/v1",
            model="qwen3.6-35b-a3b-q8-local",
            api_key="endpoint-token",
        )
        backend = QwenWebChatBackend(
            client=client,
            local_generation_endpoints={"lm-studio": endpoint},
        )
        backend._health.available = True
        backend.start = AsyncMock()

        session = QwenManagedChatSession(conversation_id="conv-qwen", _backend=backend)
        session.project_path = "/tmp/project"
        session._model = "qwen3.6-35b-a3b-q8-local(openai)"

        with patch(
            "gobby.servers.websocket.chat.backends.qwen.ensure_qwen_local_openai_model_ready",
            new=AsyncMock(),
        ) as mock_warmup:
            await backend.attach_session(session)

        mock_warmup.assert_awaited_once_with(
            "qwen3.6-35b-a3b-q8-local(openai)",
            project_path="/tmp/project",
            local_generation_endpoints={"lm-studio": endpoint},
        )
        assert mock_warmup.await_count == 1
        assert mock_warmup.await_args is not None
        resolved_project_path = str(Path(session.project_path).resolve())
        client.create_session.assert_awaited_once_with(
            model="qwen3.6-35b-a3b-q8-local(openai)",
            cwd=resolved_project_path,
            reasoning_effort=None,
        )
        assert client.create_session.await_count == 1
        assert client.create_session.await_args is not None


async def _collect_codex_backend_events(
    notifications: list[tuple[str, dict[str, Any]]],
    *,
    transcript_path: Path | None = None,
    transcript_lines: list[str] | None = None,
    is_connected: bool = True,
) -> tuple[list[Any], CodexManagedChatSession]:
    handlers: dict[str, list[Any]] = {}

    def add_handler(method: str, handler: Any) -> None:
        handlers.setdefault(method, []).append(handler)

    async def start_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
        if transcript_path is not None and transcript_lines is not None:
            transcript_path.write_text("\n".join(transcript_lines) + "\n", encoding="utf-8")
        for method, params in notifications:
            for handler in handlers.get(method, []):
                handler(method, params)
        return SimpleNamespace(id="turn-1")

    client = MagicMock()
    client.is_connected = is_connected
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.add_notification_handler = MagicMock(side_effect=add_handler)
    client.remove_notification_handler = MagicMock()
    client.start_turn = AsyncMock(side_effect=start_turn)

    backend = CodexWebChatBackend(client=client)
    await backend.start()

    session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
    session._connected = True
    session._thread_id = "thread-1"
    if transcript_path is not None:
        transcript_path.write_text("", encoding="utf-8")
        session._transcript_path = str(transcript_path)
    else:
        session._get_transcript_offset = AsyncMock(return_value=0)
        session._get_transcript_assistant_text_since = AsyncMock(return_value=None)

    events = [event async for event in backend.send_message(session, "hello")]
    return events, session


class TestCodexBackend:
    @pytest.mark.asyncio
    async def test_attach_session_refuses_without_launch_snapshot(self) -> None:
        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.start_thread = AsyncMock()

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session.project_path = "/tmp/project"
        assert session.sandbox_config is None

        with pytest.raises(RuntimeError, match="no sandbox policy snapshot"):
            await session.start(model="gpt-5.4")

        client.start_thread.assert_not_awaited()
        assert session.is_connected is False

    @pytest.mark.asyncio
    async def test_attach_session_reuses_shared_client(self) -> None:
        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.start_thread = AsyncMock(
            return_value=SimpleNamespace(id="thread-1", path="/tmp/codex.jsonl")
        )

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(
            conversation_id="conv-codex",
            _backend=backend,
            sandbox_config=SandboxConfig(enabled=False),
        )
        session.project_path = "/tmp/project"
        await session.start(model="gpt-5.4")

        client.start_thread.assert_awaited_once_with(
            cwd="/tmp/project",
            model="gpt-5.4",
            approval_policy="on-request",
            sandbox=None,
            terminal_context=None,
        )
        assert session.sdk_session_id == "thread-1"
        assert session._thread_id == "thread-1"
        assert session._transcript_path == "/tmp/codex.jsonl"

    @pytest.mark.asyncio
    async def test_web_chat_shared_client_handles_chat_without_event_leakage(self) -> None:
        class SharedCodexClient:
            def __init__(self) -> None:
                self.is_connected = True
                self.handlers: dict[str, list[Any]] = {}
                self.archived_thread_ids: list[str] = []

            async def start(self) -> None:
                self.is_connected = True

            async def stop(self) -> None:
                self.is_connected = False

            def register_approval_handler(self, _handler: Any) -> None:
                return None

            def add_notification_handler(self, method: str, handler: Any) -> None:
                self.handlers.setdefault(method, []).append(handler)

            def remove_notification_handler(self, method: str, handler: Any) -> None:
                self.handlers.get(method, []).remove(handler)

            async def start_thread(
                self,
                cwd: str | None = None,
                model: str | None = None,
                approval_policy: str | None = None,
                sandbox: str | None = None,
                terminal_context: dict[str, Any] | None = None,
            ) -> SimpleNamespace:
                return SimpleNamespace(id="one-shot-thread")

            async def start_turn(
                self,
                thread_id: str,
                prompt: str,
                **_config_overrides: Any,
            ) -> SimpleNamespace:
                notifications = [
                    (
                        "agent/messageDelta",
                        {
                            "threadId": "one-shot-thread",
                            "turnId": "one-shot-turn",
                            "delta": "one-shot leak",
                        },
                    ),
                    ("turn/started", {"threadId": thread_id, "turnId": "chat-turn"}),
                    (
                        "agent/messageDelta",
                        {"threadId": thread_id, "turnId": "chat-turn", "delta": "chat ok"},
                    ),
                    ("turn/completed", {"threadId": thread_id, "turnId": "chat-turn", "usage": {}}),
                ]
                for method, params in notifications:
                    for handler in list(self.handlers.get(method, [])):
                        handler(method, params)
                return SimpleNamespace(id="chat-turn")

            async def archive_thread(self, thread_id: str) -> None:
                self.archived_thread_ids.append(thread_id)

        async def collect_text_chunks(events: AsyncIterator[Any]) -> list[str]:
            return [event.content async for event in events if isinstance(event, TextChunk)]

        client = SharedCodexClient()
        backend = CodexWebChatBackend(client=client)
        await backend.start()
        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session._thread_id = "chat-thread"
        session.sdk_session_id = "chat-thread"
        session._get_transcript_offset = AsyncMock(return_value=0)
        session._get_transcript_records_since = AsyncMock(return_value=[])
        session._get_transcript_assistant_text_since = AsyncMock(return_value=None)
        chat_text = await collect_text_chunks(backend.send_message(session, "chat"))

        assert chat_text == ["chat ok"]
        assert client.archived_thread_ids == []

    @pytest.mark.asyncio
    async def test_attach_session_passes_codex_sandbox_policy(self) -> None:
        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.start_thread = AsyncMock(
            return_value=SimpleNamespace(id="thread-1", path="/tmp/codex.jsonl")
        )

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(
            conversation_id="conv-codex",
            _backend=backend,
            sandbox_config=SandboxConfig(enabled=True, mode="restrictive"),
        )
        session.project_path = "/tmp/project"
        await session.start(model="gpt-5.4")

        client.start_thread.assert_awaited_once_with(
            cwd="/tmp/project",
            model="gpt-5.4",
            approval_policy="on-request",
            sandbox="read-only",
            terminal_context=None,
        )
        assert client.start_thread.await_count == 1
        assert client.start_thread.await_args is not None

    @pytest.mark.asyncio
    async def test_attach_session_preflights_local_codex_model(self) -> None:
        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.start_thread = AsyncMock(
            return_value=SimpleNamespace(id="thread-1", path="/tmp/codex.jsonl")
        )
        endpoint = GenerationEndpointConfig(
            protocol="ollama",
            api_base="http://localhost:11434",
            model="llama3.2:latest",
        )

        backend = CodexWebChatBackend(client=client, generation_endpoint=endpoint)
        await backend.start()

        session = CodexManagedChatSession(
            conversation_id="conv-codex",
            _backend=backend,
            sandbox_config=SandboxConfig(enabled=False),
        )
        session.project_path = "/tmp/project"
        with patch(
            "gobby.servers.websocket.chat.backends.codex.ensure_local_model",
            new=AsyncMock(return_value="ollama/qwen3-coder"),
        ) as ensure_local_model:
            await session.start(model="ollama/qwen3-coder")

        ensure_local_model.assert_awaited_once()
        assert ensure_local_model.await_args.args[0].model == "ollama/qwen3-coder"
        assert ensure_local_model.await_args.args[0].api_base == "http://localhost:11434"
        assert ensure_local_model.await_args.kwargs == {"run_manager": None}
        client.start_thread.assert_awaited_once_with(
            cwd="/tmp/project",
            model="ollama/qwen3-coder",
            approval_policy="on-request",
            sandbox=None,
            terminal_context=None,
        )
        assert session.sdk_session_id == "thread-1"
        assert session._thread_id == "thread-1"
        assert session._transcript_path == "/tmp/codex.jsonl"

    @pytest.mark.asyncio
    async def test_attach_session_wraps_local_codex_preflight_failure_context(self) -> None:
        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.start_thread = AsyncMock(
            return_value=SimpleNamespace(id="thread-1", path="/tmp/codex.jsonl")
        )
        endpoint = GenerationEndpointConfig(
            protocol="ollama",
            api_base="http://localhost:11434",
            model="llama3.2:latest",
            api_key="secret-token",
        )
        backend = CodexWebChatBackend(client=client, generation_endpoint=endpoint)
        await backend.start()
        session = CodexManagedChatSession(
            conversation_id="conv-codex",
            _backend=backend,
            sandbox_config=SandboxConfig(enabled=False),
        )
        session.project_path = "/tmp/project"
        failure = LocalModelError("model not loaded")

        with (
            patch(
                "gobby.servers.websocket.chat.backends.codex.ensure_local_model",
                new=AsyncMock(side_effect=failure),
            ) as ensure_local_model,
            pytest.raises(RuntimeError) as exc_info,
        ):
            await session.start(model="ollama/qwen3-coder")

        ensure_local_model.assert_awaited_once()
        message = str(exc_info.value)
        assert "protocol=ollama" in message
        assert "model=ollama/qwen3-coder" in message
        # The resolver's diagnosis is the actionable part (#20646); credentials
        # never travel with it.
        assert message.endswith(": model not loaded")
        assert "api_base=http://localhost:11434" not in message
        assert "secret-token" not in message
        assert "api_key" not in message
        assert exc_info.value.__cause__ is failure
        client.start_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_managed_session_delegates_send_message(self) -> None:
        backend = MagicMock()
        backend.attach_session = AsyncMock()
        backend.send_message = MagicMock(
            return_value=_async_stream(
                TextChunk(content="codex ok"),
                DoneEvent(tool_calls_count=0, sdk_session_id="thread-1"),
            )
        )

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session.sdk_session_id = "thread-1"

        events = [event async for event in session.send_message("hello")]

        assert session.message_index == 1
        assert [e.content for e in events if isinstance(e, TextChunk)] == ["codex ok"]

    @pytest.mark.asyncio
    async def test_send_message_stops_when_app_server_disconnects(self) -> None:
        events, _session = await _collect_codex_backend_events([], is_connected=False)

        assert isinstance(events[0], TextChunk)
        assert events[0].content == "Error: Codex app-server disconnected before turn completed"
        assert isinstance(events[-1], DoneEvent)

    @pytest.mark.asyncio
    async def test_send_message_stops_at_turn_deadline(self) -> None:
        with patch(
            "gobby.servers.websocket.chat.backends.codex_turns._CODEX_TURN_TIMEOUT_SECONDS",
            0.001,
        ):
            events, _session = await _collect_codex_backend_events([])

        assert isinstance(events[0], TextChunk)
        assert events[0].content == "Error: Codex turn timed out after 0.001 seconds"
        assert isinstance(events[-1], DoneEvent)

    @pytest.mark.asyncio
    async def test_send_message_passes_request_scoped_turn_overrides(self) -> None:
        handlers: dict[str, list[Any]] = {}

        def add_handler(method: str, handler: Any) -> None:
            handlers.setdefault(method, []).append(handler)

        async def start_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
            for handler in handlers.get("turn/completed", []):
                handler(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                )
            return SimpleNamespace(id="turn-1")

        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.add_notification_handler = MagicMock(side_effect=add_handler)
        client.remove_notification_handler = MagicMock()
        client.start_turn = AsyncMock(side_effect=start_turn)

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session._thread_id = "thread-1"
        session._model = "gpt-5.4"
        session._context_window_overrides = {"gpt-5.4": 200_000}
        session.reasoning_effort = "xhigh"
        session._get_transcript_offset = AsyncMock(return_value=0)
        session._get_transcript_assistant_text_since = AsyncMock(return_value=None)

        events = [
            event
            async for event in backend.send_message(
                session,
                "hello",
                request_parameters={"serviceTier": "priority"},
            )
        ]

        client.start_turn.assert_awaited_once_with(
            "thread-1",
            "hello",
            context_prefix=None,
            model="gpt-5.4",
            effort="xhigh",
            serviceTier="priority",
        )
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].context_window == 200_000

    @pytest.mark.asyncio
    async def test_send_message_emits_tool_call_event_for_started_item(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "item/started",
                    {
                        "threadId": "thread-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "list_tasks",
                                "arguments": '{"status":"open"}',
                            },
                        },
                    },
                ),
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                ),
            ]
        )

        tool_calls = [event for event in events if isinstance(event, ToolCallEvent)]

        assert len(tool_calls) == 1
        assert tool_calls[0].tool_call_id == "item-mcp-1"
        assert tool_calls[0].tool_name == "mcp__gobby-tasks__list_tasks"
        assert tool_calls[0].server_name == "gobby-tasks"
        assert tool_calls[0].arguments == {"status": "open"}
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_send_message_emits_tool_result_event_for_completed_item(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "item/started",
                    {
                        "threadId": "thread-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "get_task",
                                "arguments": '{"task_id":"#42"}',
                            },
                        },
                    },
                ),
                (
                    "item/completed",
                    {
                        "threadId": "thread-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "get_task",
                                "arguments": '{"task_id":"#42"}',
                            },
                            "result": {"id": "#42", "title": "Fix chat"},
                        },
                    },
                ),
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                ),
            ]
        )

        tool_results = [event for event in events if isinstance(event, ToolResultEvent)]

        assert len(tool_results) == 1
        assert tool_results[0].tool_call_id == "item-mcp-1"
        assert tool_results[0].success is False
        assert tool_results[0].result == {"id": "#42", "title": "Fix chat"}
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_send_message_emits_fallback_tool_call_for_completed_item(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "item/completed",
                    {
                        "threadId": "thread-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "close_task",
                                "arguments": '{"task_id":"#42"}',
                            },
                            "result": {"success": True},
                        },
                    },
                ),
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                ),
            ]
        )

        assert [type(event) for event in events] == [ToolCallEvent, ToolResultEvent, DoneEvent]
        assert events[0].tool_call_id == "item-mcp-1"
        assert events[1].tool_call_id == "item-mcp-1"
        assert events[1].result == {"success": True}
        assert events[2].tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_send_message_normalizes_camel_and_snake_case_usage(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {
                            "inputTokens": 5,
                            "output_tokens": 7,
                            "cacheReadInputTokens": 11,
                            "cache_creation_input_tokens": 13,
                        },
                    },
                ),
            ]
        )

        done = events[-1]

        assert isinstance(done, DoneEvent)
        assert done.input_tokens == 5
        assert done.output_tokens == 7
        assert done.cache_read_input_tokens == 11
        assert done.cache_creation_input_tokens == 13
        assert done.total_input_tokens == 29

    @pytest.mark.asyncio
    async def test_send_message_emits_tool_events_from_codex_response_items(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "response_item",
                    {
                        "type": "function_call",
                        "name": "call_tool",
                        "arguments": json.dumps(
                            {
                                "server_name": "gobby-tasks",
                                "tool_name": "get_task",
                                "arguments": {"task_id": "#42"},
                            }
                        ),
                        "call_id": "call-1",
                    },
                ),
                (
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": json.dumps({"success": True}),
                    },
                ),
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                ),
            ]
        )

        tool_call = next(event for event in events if isinstance(event, ToolCallEvent))
        tool_result = next(event for event in events if isinstance(event, ToolResultEvent))

        assert tool_call.tool_call_id == "call-1"
        assert tool_call.tool_name == "call_tool"
        assert tool_call.server_name == "gobby-tasks"
        assert tool_call.arguments["tool_name"] == "get_task"
        assert tool_result.tool_call_id == "call-1"
        assert tool_result.result == {"success": True}
        assert events[-1].tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_send_message_uses_token_count_event_for_done_usage(self) -> None:
        events, _session = await _collect_codex_backend_events(
            [
                (
                    "event_msg",
                    {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 26_435,
                                "cached_input_tokens": 25_984,
                                "output_tokens": 10,
                                "reasoning_output_tokens": 2,
                            },
                            "model_context_window": 258_400,
                        },
                    },
                ),
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                ),
            ]
        )

        done = events[-1]

        assert isinstance(done, DoneEvent)
        assert done.input_tokens == 451
        assert done.cache_read_input_tokens == 25_984
        assert done.output_tokens == 10
        assert done.total_input_tokens == 26_435
        assert done.context_window == 258_400

    @pytest.mark.asyncio
    async def test_send_message_recovers_tool_and_usage_from_codex_transcript(
        self,
        tmp_path: Path,
    ) -> None:
        transcript_path = tmp_path / "codex.jsonl"
        transcript_lines = [
            json.dumps(
                {
                    "timestamp": "2026-05-13T18:26:09.726Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "call_tool",
                        "arguments": json.dumps(
                            {
                                "server_name": "gobby-tasks",
                                "tool_name": "get_task",
                                "arguments": {"task_id": "#14579"},
                            }
                        ),
                        "call_id": "call-1",
                    },
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-05-13T18:26:10.077Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "mcp_tool_call_end",
                        "call_id": "call-1",
                        "invocation": {
                            "server": "gobby",
                            "tool": "call_tool",
                            "arguments": {
                                "server_name": "gobby-tasks",
                                "tool_name": "get_task",
                                "arguments": {"task_id": "#14579"},
                            },
                        },
                        "result": {"Ok": {"structuredContent": {"success": True}}},
                    },
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-05-13T18:26:13.962Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 128_050,
                                "cached_input_tokens": 108_416,
                                "output_tokens": 214,
                                "reasoning_output_tokens": 82,
                            },
                            "model_context_window": 258_400,
                        },
                    },
                }
            ),
        ]

        events, _session = await _collect_codex_backend_events(
            [
                (
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                ),
            ],
            transcript_path=transcript_path,
            transcript_lines=transcript_lines,
        )

        tool_calls = [event for event in events if isinstance(event, ToolCallEvent)]
        tool_results = [event for event in events if isinstance(event, ToolResultEvent)]
        done = events[-1]

        assert len(tool_calls) == 1
        assert tool_calls[0].tool_call_id == "call-1"
        assert tool_calls[0].tool_name == "call_tool"
        assert len(tool_results) == 1
        assert tool_results[0].tool_call_id == "call-1"
        assert isinstance(done, DoneEvent)
        assert done.tool_calls_count == 1
        assert done.input_tokens == 19_634
        assert done.cache_read_input_tokens == 108_416
        assert done.output_tokens == 214
        assert done.total_input_tokens == 128_050
        assert done.context_window == 258_400

    @pytest.mark.asyncio
    async def test_interrupt_uses_thread_and_turn_identity(self) -> None:
        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.interrupt_turn = AsyncMock()

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._thread_id = "thread-1"
        session._turn_id = "turn-9"

        await backend.interrupt(session)

        client.interrupt_turn.assert_awaited_once_with("thread-1", "turn-9")
        assert client.interrupt_turn.await_count == 1
        assert client.interrupt_turn.await_args is not None
        assert session._turn_id is None

    @pytest.mark.asyncio
    async def test_stale_turn_interrupt_logs_debug_not_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An interrupt arriving after the turn advanced is benign: the target
        turn is already gone, so it logs at DEBUG and re-raises CancelledError."""
        import logging as py_logging

        from gobby.servers.websocket.chat.backends.codex_turns import stream_codex_turn

        client = MagicMock()
        client.is_connected = True
        client.start_turn = AsyncMock(side_effect=asyncio.CancelledError())
        client.interrupt_turn = AsyncMock(
            side_effect=RuntimeError("expected active turn id turn-9 but found turn-10")
        )

        session = MagicMock()
        session.conversation_id = "conv-codex"
        session._thread_id = "thread-1"
        session._turn_id = "turn-9"
        session._model = None
        session.reasoning_effort = None
        session._get_transcript_offset = AsyncMock(return_value=0)
        session._reset_before_tool_state = MagicMock()

        with (
            caplog.at_level(
                py_logging.DEBUG, logger="gobby.servers.websocket.chat.backends.codex_turns"
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            async for _ in stream_codex_turn(
                client=client,
                session=session,
                prompt="hello",
                context_prefix=None,
                extract_before_tool_dedup_key=lambda _params: None,
            ):
                pass

        records = [
            record
            for record in caplog.records
            if record.name == "gobby.servers.websocket.chat.backends.codex_turns"
        ]
        assert not any(record.levelno >= py_logging.WARNING for record in records)
        assert any(
            record.levelno == py_logging.DEBUG
            and "already finished before interrupt" in record.message
            for record in records
        )

    @pytest.mark.asyncio
    async def test_other_interrupt_runtime_errors_still_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-race RuntimeErrors from interrupt_turn keep the WARNING path."""
        import logging as py_logging

        from gobby.servers.websocket.chat.backends.codex_turns import stream_codex_turn

        client = MagicMock()
        client.is_connected = True
        client.start_turn = AsyncMock(side_effect=asyncio.CancelledError())
        client.interrupt_turn = AsyncMock(side_effect=RuntimeError("connection reset"))

        session = MagicMock()
        session.conversation_id = "conv-codex"
        session._thread_id = "thread-1"
        session._turn_id = "turn-9"
        session._model = None
        session.reasoning_effort = None
        session._get_transcript_offset = AsyncMock(return_value=0)
        session._reset_before_tool_state = MagicMock()

        with (
            caplog.at_level(
                py_logging.DEBUG, logger="gobby.servers.websocket.chat.backends.codex_turns"
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            async for _ in stream_codex_turn(
                client=client,
                session=session,
                prompt="hello",
                context_prefix=None,
                extract_before_tool_dedup_key=lambda _params: None,
            ):
                pass

        warnings = [
            record
            for record in caplog.records
            if record.name == "gobby.servers.websocket.chat.backends.codex_turns"
            and record.levelno == py_logging.WARNING
        ]
        assert len(warnings) == 1
        assert "Failed to interrupt cancelled Codex turn" in warnings[0].message

    @pytest.mark.asyncio
    async def test_transcript_retry_uses_configured_timing(self, tmp_path: Path) -> None:
        transcript = tmp_path / "codex.jsonl"
        transcript.write_text("ignored\n", encoding="utf-8")

        session = CodexManagedChatSession(
            conversation_id="conv-codex",
            _backend=MagicMock(),
            _thread_id="thread-1",
            _transcript_path=str(transcript),
            _transcript_retry_attempts=2,
            _transcript_retry_delay_seconds=0.25,
        )

        recovered = ParsedMessage(
            index=0,
            role="assistant",
            content="Recovered from transcript",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(UTC),
            raw_json={},
        )
        parsed_batches = [
            [],
            [recovered],
        ]

        sleep = AsyncMock()
        with (
            patch(
                "gobby.servers.websocket.chat.backends.codex.CodexTranscriptParser.parse_lines",
                side_effect=parsed_batches,
            ),
            patch("gobby.servers.websocket.chat.backends.codex.asyncio.sleep", sleep),
        ):
            assistant_text = await session._get_transcript_assistant_text_since(0)

        assert assistant_text == "Recovered from transcript"
        sleep.assert_awaited_once_with(0.25)
        assert sleep.await_count == 1
        assert sleep.await_args is not None

    @pytest.mark.asyncio
    async def test_handle_approval_request_accepts_decision_dict(self) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(
            conversation_id="conv-codex",
            _backend=backend,
            sandbox_config=SandboxConfig(enabled=False),
        )
        session.project_path = "/tmp/project"
        session.chat_mode = "accept_edits"
        session._thread_id = "thread-1"
        session._wait_for_tool_approval = AsyncMock(return_value={"decision": "accept"})
        backend._sessions_by_thread["thread-1"] = session

        with (
            patch.object(
                backend,
                "_translate_approval_request",
                return_value=("Write", {"file_path": "notes.md"}),
            ),
            patch(
                "gobby.servers.websocket.chat.backends.codex.find_out_of_repo_write_path",
                return_value=None,
            ),
            patch(
                "gobby.servers.websocket.chat.backends.codex.is_tool_auto_allowed",
                return_value=False,
            ),
        ):
            result = await backend.handle_approval_request("tools/call", {"threadId": "thread-1"})

        assert result == backend._accept_response("tools/call")
        assert session._wait_for_tool_approval.await_count == 1

    @pytest.mark.asyncio
    async def test_handle_approval_request_respects_managed_pre_tool_block(self) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(
            conversation_id="conv-codex",
            _backend=backend,
            sandbox_config=SandboxConfig(enabled=False),
        )
        session.project_path = "/tmp/project"
        session.chat_mode = "accept_edits"
        session._thread_id = "thread-1"
        session._on_pre_tool = AsyncMock(
            return_value={"decision": "block", "context": TASKS_SKILL_DIRECTIVE}
        )
        backend._sessions_by_thread["thread-1"] = session

        with (
            patch.object(
                backend,
                "_translate_approval_request",
                return_value=(
                    "mcp__gobby__call_tool",
                    {"server_name": "gobby-tasks", "tool_name": "close_task"},
                ),
            ),
            patch(
                "gobby.servers.websocket.chat.backends.codex.find_out_of_repo_write_path",
                return_value=None,
            ),
        ):
            result = await backend.handle_approval_request(
                "mcpServer/elicitation/request",
                {"threadId": "thread-1"},
            )

        assert result == backend._decline_response("mcpServer/elicitation/request")
        session._on_pre_tool.assert_awaited_once_with(
            {
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {"server_name": "gobby-tasks", "tool_name": "close_task"},
            }
        )
        assert session._consume_deferred_context() == TASKS_SKILL_DIRECTIVE

    @pytest.mark.asyncio
    async def test_handle_approval_request_allows_gcode_in_plan_mode(self) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(
            conversation_id="conv-codex",
            _backend=backend,
            sandbox_config=SandboxConfig(enabled=False),
        )
        session.project_path = "/tmp/project"
        session.chat_mode = "plan"
        session._thread_id = "thread-1"
        backend._sessions_by_thread["thread-1"] = session

        result = await backend.handle_approval_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "parsedCmd": 'gcode search "ChatSession"'},
        )

        assert result == backend._accept_response("item/commandExecution/requestApproval")

    @pytest.mark.asyncio
    async def test_handle_approval_request_blocks_gcode_redirection_in_plan_mode(self) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(
            conversation_id="conv-codex",
            _backend=backend,
            sandbox_config=SandboxConfig(enabled=False),
        )
        session.project_path = "/tmp/project"
        session.chat_mode = "plan"
        session._thread_id = "thread-1"
        backend._sessions_by_thread["thread-1"] = session

        result = await backend.handle_approval_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "parsedCmd": 'gcode search "ChatSession" > notes.txt'},
        )

        assert result == backend._decline_response("item/commandExecution/requestApproval")

    @pytest.mark.asyncio
    async def test_handle_approval_request_allows_codex_scratch_write_in_plan_mode(
        self,
        tmp_path: Path,
    ) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        repo = tmp_path / "repo"
        repo.mkdir()
        session.project_path = str(repo)
        session.chat_mode = "plan"
        session._thread_id = "thread-1"
        backend._sessions_by_thread["thread-1"] = session
        target = str(Path.home() / ".codex" / "scratch" / "state.json")

        result = await backend.handle_approval_request(
            "item/fileChange/requestApproval",
            {"threadId": "thread-1", "changes": [{"path": target}]},
        )

        assert result == backend._accept_response("item/fileChange/requestApproval")

    @pytest.mark.asyncio
    async def test_handle_approval_request_blocks_project_local_codex_config(
        self,
        tmp_path: Path,
    ) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        repo = tmp_path / "repo"
        repo.mkdir()
        session.project_path = str(repo)
        session.chat_mode = "plan"
        session._thread_id = "thread-1"
        backend._sessions_by_thread["thread-1"] = session

        result = await backend.handle_approval_request(
            "item/fileChange/requestApproval",
            {"threadId": "thread-1", "changes": [{"path": ".codex/plans/project-local.md"}]},
        )

        assert result == backend._decline_response("item/fileChange/requestApproval")

    @pytest.mark.asyncio
    async def test_handle_approval_request_blocks_mixed_codex_plan_write(
        self,
        tmp_path: Path,
    ) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        repo = tmp_path / "repo"
        repo.mkdir()
        session.project_path = str(repo)
        session.chat_mode = "plan"
        session._thread_id = "thread-1"
        backend._sessions_by_thread["thread-1"] = session
        scratch = str(Path.home() / ".codex" / "scratch" / "state.json")

        result = await backend.handle_approval_request(
            "item/fileChange/requestApproval",
            {
                "threadId": "thread-1",
                "changes": [{"path": scratch}, {"path": str(repo / "src" / "unsafe.py")}],
            },
        )

        assert result == backend._decline_response("item/fileChange/requestApproval")

    @pytest.mark.asyncio
    async def test_handle_approval_request_blocks_codex_scratch_write_in_normal_mode(
        self,
        tmp_path: Path,
    ) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        repo = tmp_path / "repo"
        repo.mkdir()
        session.project_path = str(repo)
        session.chat_mode = "normal"
        session._thread_id = "thread-1"
        backend._sessions_by_thread["thread-1"] = session
        target = str(Path.home() / ".codex" / "scratch" / "state.json")

        result = await backend.handle_approval_request(
            "item/fileChange/requestApproval",
            {"threadId": "thread-1", "changes": [{"path": target}]},
        )

        assert result == backend._decline_response("item/fileChange/requestApproval")

    @pytest.mark.asyncio
    async def test_send_message_replays_deferred_context_prefix(self) -> None:
        backend = MagicMock()
        backend.attach_session = AsyncMock()
        backend.send_message = MagicMock(
            return_value=_async_stream(DoneEvent(tool_calls_count=0, sdk_session_id="thread-1"))
        )

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session._thread_id = "thread-1"
        session._deferred_contexts.append(CODE_INDEX_SKILL_DIRECTIVE)

        events = [event async for event in session.send_message("hello")]

        assert isinstance(events[-1], DoneEvent)
        assert backend.send_message.call_args.kwargs["context_prefix"] is not None
        assert CODE_INDEX_SKILL_DIRECTIVE in backend.send_message.call_args.kwargs["context_prefix"]

    @pytest.mark.asyncio
    async def test_send_message_applies_post_tool_lifecycle_for_completed_items(self) -> None:
        handlers: dict[str, list[Any]] = {}

        def add_handler(method: str, handler: Any) -> None:
            handlers.setdefault(method, []).append(handler)

        async def start_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
            response_items = [
                {
                    "type": "function_call",
                    "name": "call_tool",
                    "arguments": json.dumps(
                        {
                            "server_name": "gobby-tasks",
                            "tool_name": "close_task",
                            "arguments": {"task_id": "#42"},
                        }
                    ),
                    "call_id": "call-lifecycle-1",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-lifecycle-1",
                    "output": json.dumps({"success": True}),
                },
            ]
            for params in response_items:
                for handler in handlers.get("response_item", []):
                    handler("response_item", params)
            for handler in handlers.get("item/completed", []):
                handler(
                    "item/completed",
                    {
                        "threadId": "thread-1",
                        "item": {
                            "id": "call-lifecycle-1",
                            "type": "mcpToolCall",
                            "server": "gobby-tasks",
                            "tool": "close_task",
                            "arguments": {"task_id": "#42"},
                            "result": {"success": True},
                        },
                    },
                )
            for handler in handlers.get("turn/completed", []):
                handler(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                )
            return SimpleNamespace(id="turn-1")

        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.add_notification_handler = MagicMock(side_effect=add_handler)
        client.remove_notification_handler = MagicMock()
        client.start_turn = AsyncMock(side_effect=start_turn)

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session._thread_id = "thread-1"
        session._on_post_tool = AsyncMock(return_value={"context": TASKS_SKILL_DIRECTIVE})
        session._get_transcript_offset = AsyncMock(return_value=0)
        session._get_transcript_assistant_text_since = AsyncMock(return_value=None)

        events = [event async for event in backend.send_message(session, "hello")]

        assert isinstance(events[-1], DoneEvent)
        session._on_post_tool.assert_awaited_once_with(
            {
                "tool_name": "mcp__gobby-tasks__close_task",
                "tool_input": {"task_id": "#42"},
                "tool_response": {"success": True},
                "is_error": False,
                "mcp_server": "gobby-tasks",
                "mcp_tool": "close_task",
            }
        )
        assert session._consume_deferred_context() == TASKS_SKILL_DIRECTIVE

    @pytest.mark.asyncio
    async def test_send_message_normalizes_realistic_completed_mcp_items(self) -> None:
        handlers: dict[str, list[Any]] = {}

        def add_handler(method: str, handler: Any) -> None:
            handlers.setdefault(method, []).append(handler)

        async def start_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
            completed_events = [
                {
                    "threadId": "thread-other",
                    "item": {
                        "id": "item-other",
                        "type": "mcpToolCall",
                        "status": "completed",
                        "mcpToolCall": {
                            "server": "gobby-tasks",
                            "tool": "close_task",
                            "arguments": '{"task_id":"#7"}',
                        },
                        "result": {"success": True},
                    },
                },
                {
                    "threadId": "thread-1",
                    "item": {
                        "id": "item-compact-1",
                        "type": ">>>contextCompaction<<<",
                        "status": "completed",
                    },
                },
                {
                    "threadId": "thread-1",
                    "item": {
                        "id": "item-msg-1",
                        "type": "assistantMessage",
                        "status": "completed",
                        "assistantMessage": {"content": [{"type": "output_text", "text": "hello"}]},
                    },
                },
                {
                    "threadId": "thread-1",
                    "item": {
                        "id": "item-mcp-1",
                        "type": "mcpToolCall",
                        "status": "completed",
                        "mcpToolCall": {
                            "server": "gobby-tasks",
                            "tool": "close_task",
                            "arguments": '{"task_id":"#42","changes_summary":"done"}',
                        },
                        "result": {"success": True, "task_id": "#42"},
                    },
                },
            ]
            for params in completed_events:
                for handler in handlers.get("item/completed", []):
                    handler("item/completed", params)
            for handler in handlers.get("turn/completed", []):
                handler(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1"},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                )
            return SimpleNamespace(id="turn-1")

        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.add_notification_handler = MagicMock(side_effect=add_handler)
        client.remove_notification_handler = MagicMock()
        client.start_turn = AsyncMock(side_effect=start_turn)

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session._thread_id = "thread-1"
        session._on_post_tool = AsyncMock()
        any_session = cast(Any, session)
        any_session._get_transcript_offset = AsyncMock(return_value=0)
        any_session._get_transcript_assistant_text_since = AsyncMock(return_value=None)

        events = [event async for event in backend.send_message(session, "close the task")]

        assert isinstance(events[-1], DoneEvent)
        session._on_post_tool.assert_awaited_once_with(
            {
                "tool_name": "mcp__gobby-tasks__close_task",
                "tool_input": {"task_id": "#42", "changes_summary": "done"},
                "tool_response": {"success": True, "task_id": "#42"},
                "is_error": False,
                "mcp_server": "gobby-tasks",
                "mcp_tool": "close_task",
            }
        )
        assert session._on_post_tool.await_count == 1
        assert session._on_post_tool.await_args is not None

    def test_translate_approval_request_parses_json_string_arguments_for_mcp_tool_call(
        self,
    ) -> None:
        backend = CodexWebChatBackend(client=MagicMock())

        tool_name, input_data = backend._translate_approval_request(
            "item/mcpToolCall/requestApproval",
            {
                "threadId": "thread-1",
                "itemId": "item-mcp-1",
                "serverName": "gobby-tasks",
                "name": "close_task",
                "arguments": '{"task_id":"#42","changes_summary":"done"}',
            },
        )

        assert tool_name == "mcp__gobby__call_tool"
        assert input_data == {
            "task_id": "#42",
            "changes_summary": "done",
            "server_name": "gobby-tasks",
            "tool_name": "close_task",
        }

    @pytest.mark.asyncio
    async def test_mcp_elicitation_preserves_nested_gobby_target(self) -> None:
        backend = CodexWebChatBackend(client=MagicMock())
        session = CodexManagedChatSession(
            conversation_id="conv-codex",
            _backend=backend,
            sandbox_config=SandboxConfig(enabled=False),
        )
        session.project_path = "/tmp/project"
        session.chat_mode = "accept_edits"
        session._thread_id = "thread-1"
        session._on_pre_tool = AsyncMock()
        wait_for_tool_approval = AsyncMock()
        object.__setattr__(session, "_wait_for_tool_approval", wait_for_tool_approval)
        backend._sessions_by_thread["thread-1"] = session

        params = {
            "threadId": "thread-1",
            "elicitationId": "elicitation-1",
            "serverName": "gobby",
            "message": 'Allow the gobby MCP server to run tool "call_tool"?',
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "tool_params": {
                    "server_name": "gobby-tasks",
                    "tool_name": "list_tasks",
                    "arguments": {"project": "_personal"},
                },
            },
        }

        result = await backend.handle_approval_request("mcpServer/elicitation/request", params)

        assert result == backend._accept_response("mcpServer/elicitation/request")
        session._on_pre_tool.assert_awaited_once_with(
            {
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "list_tasks",
                    "arguments": {"project": "_personal"},
                },
            }
        )
        wait_for_tool_approval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_message_dispatches_pre_tool_once_per_item_and_resets_each_turn(
        self,
    ) -> None:
        handlers: dict[str, list[Any]] = {}
        turn_count = 0

        def add_handler(method: str, handler: Any) -> None:
            handlers.setdefault(method, []).append(handler)

        async def start_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
            nonlocal turn_count
            turn_count += 1
            turn_id = f"turn-{turn_count}"
            for handler in handlers.get("turn/started", []):
                handler("turn/started", {"threadId": "thread-1", "turn": {"id": turn_id}})
            for handler in handlers.get("item/started", []):
                handler(
                    "item/started",
                    {
                        "threadId": "thread-1",
                        "turnId": turn_id,
                        "itemId": "item-mcp-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "close_task",
                                "arguments": '{"task_id":"#42"}',
                            },
                        },
                    },
                )
                handler(
                    "item/started",
                    {
                        "threadId": "thread-1",
                        "turnId": turn_id,
                        "itemId": "item-mcp-1",
                        "item": {
                            "id": "item-mcp-1",
                            "type": "mcpToolCall",
                            "mcpToolCall": {
                                "server": "gobby-tasks",
                                "tool": "close_task",
                                "arguments": '{"task_id":"#42"}',
                            },
                        },
                    },
                )
            for handler in handlers.get("turn/completed", []):
                handler(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": turn_id},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    },
                )
            return SimpleNamespace(id=turn_id)

        client = MagicMock()
        client.is_connected = True
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.add_notification_handler = MagicMock(side_effect=add_handler)
        client.remove_notification_handler = MagicMock()
        client.start_turn = AsyncMock(side_effect=start_turn)

        backend = CodexWebChatBackend(client=client)
        await backend.start()

        session = CodexManagedChatSession(conversation_id="conv-codex", _backend=backend)
        session._connected = True
        session._thread_id = "thread-1"
        session.chat_mode = "bypass"
        session._on_pre_tool = AsyncMock()
        object.__setattr__(
            session,
            "_get_transcript_offset",
            AsyncMock(return_value=0),
        )
        object.__setattr__(
            session,
            "_get_transcript_assistant_text_since",
            AsyncMock(return_value=None),
        )
        backend._sessions_by_thread["thread-1"] = session

        [event async for event in backend.send_message(session, "first turn")]
        with patch(
            "gobby.servers.websocket.chat.backends.codex.find_out_of_repo_write_path",
            return_value=None,
        ):
            approval_result = await backend.handle_approval_request(
                "item/mcpToolCall/requestApproval",
                {
                    "threadId": "thread-1",
                    "itemId": "item-mcp-1",
                    "serverName": "gobby-tasks",
                    "name": "close_task",
                    "arguments": '{"task_id":"#42"}',
                },
            )
        [event async for event in backend.send_message(session, "second turn")]

        assert approval_result == backend._accept_response("item/mcpToolCall/requestApproval")
        assert session._on_pre_tool.await_args_list == [
            call(
                {
                    "tool_name": "mcp__gobby-tasks__close_task",
                    "tool_input": {"task_id": "#42"},
                }
            ),
            call(
                {
                    "tool_name": "mcp__gobby-tasks__close_task",
                    "tool_input": {"task_id": "#42"},
                }
            ),
        ]
