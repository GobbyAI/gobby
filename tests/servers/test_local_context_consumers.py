from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.app import DaemonConfig
from gobby.llm import local as local_module
from gobby.llm.base import LLMTextResult
from gobby.llm.context_windows import resolve_context_window
from gobby.llm.local import LocalLLMProvider
from gobby.providers.capabilities.local_context import (
    ContextDiagnostic,
    LocalContextInstance,
    LocalContextObservation,
)
from gobby.providers.capabilities.local_context_config import LocalContextRoute
from gobby.servers.chat_session import ChatSession
from gobby.servers.chat_session_messages import ChatSessionMessagesMixin
from gobby.servers.websocket.chat import runtime_manager as runtime_manager_module
from gobby.servers.websocket.chat.backends import CodexManagedChatSession, QwenManagedChatSession
from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager

pytestmark = pytest.mark.unit


class _ContextService:
    def __init__(self, limits: dict[tuple[str, str], int | None]) -> None:
        self.limits = limits
        self.events: list[tuple[str, str, str]] = []

    async def refresh(self, route: LocalContextRoute) -> LocalContextObservation:
        self.events.append(("refresh", route.endpoint_id, route.model_id))
        limit = self.limits[(route.endpoint_id, route.model_id)]
        return _observation(route, limit)


class _RecordingAdapter:
    def __init__(self, events: list[tuple[str, str, str]]) -> None:
        self.events = events
        self.client = None

    async def generate_text_result(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        reasoning_effort: str | None = None,
        images: list[str] | None = None,
    ) -> LLMTextResult:
        del prompt, system_prompt, max_tokens, reasoning_effort, images
        self.events.append(("execute", "text", model or ""))
        return LLMTextResult(text="ok")

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        del prompt, system_prompt, max_tokens, reasoning_effort
        self.events.append(("execute", "json", model or ""))
        return {"ok": True}


class _MessageContextProjection(ChatSessionMessagesMixin):
    def __init__(
        self,
        route: LocalContextRoute,
        observation: LocalContextObservation,
    ) -> None:
        self._last_model = route.model_id
        self._context_window_overrides = {}
        self._local_context_route = route
        self._local_context_observation = observation


def _observation(route: LocalContextRoute, limit: int | None) -> LocalContextObservation:
    failed = limit is None
    return LocalContextObservation(
        machine_id=route.machine_id,
        endpoint_id=route.endpoint_id,
        configuration_fingerprint=route.configuration_fingerprint,
        provider=route.provider,
        model_id=route.model_id,
        canonical_limit=limit * 2 if limit is not None else None,
        provenance={"runtime_limit": "test:runtime"} if limit is not None else {},
        diagnostics=(ContextDiagnostic.ENDPOINT_UNAVAILABLE,) if failed else (),
        instances=(LocalContextInstance(model_id=route.model_id, runtime_limit=limit),)
        if limit is not None
        else (),
    )


def _config() -> DaemonConfig:
    return DaemonConfig(
        web_chat_sandbox={"enabled": False},
        ai={
            "generation": {
                "endpoints": {
                    "generation": {
                        "protocol": "lmstudio",
                        "api_base": "http://localhost:1234/v1",
                        "model": "generator",
                    },
                    "first": {
                        "protocol": "lmstudio",
                        "api_base": "http://127.0.0.1:1234/v1",
                        "model": "same",
                    },
                    "second": {
                        "protocol": "lmstudio",
                        "api_base": "http://127.0.0.1:2234/v1",
                        "model": "same",
                    },
                    "remote": {
                        "protocol": "lmstudio",
                        "api_base": "https://models.example.test/v1",
                        "model": "same",
                    },
                }
            }
        },
    )


def _install_context(
    monkeypatch: pytest.MonkeyPatch,
    service: _ContextService,
) -> None:
    context = SimpleNamespace(local_context_service=service)
    monkeypatch.setattr("gobby.app_context.get_app_context", lambda: context)
    monkeypatch.setattr("gobby.utils.machine_id.require_machine_id", lambda: "machine")
    monkeypatch.setattr(runtime_manager_module, "CodexAppServerClient", MagicMock)


@pytest.mark.asyncio
async def test_generation_and_chat_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    service = _ContextService(
        {
            ("endpoint:generation", "generator"): 32_768,
            ("endpoint:first", "same"): 65_536,
        }
    )
    _install_context(monkeypatch, service)
    execution_events: list[tuple[str, str, str]] = []
    adapter = _RecordingAdapter(execution_events)
    monkeypatch.setattr(local_module, "create_local_provider_adapter", lambda _endpoint: adapter)
    provider = LocalLLMProvider(config, endpoint_name="generation")

    assert await provider.generate_text("hello") == "ok"
    assert service.events[-1] == ("refresh", "endpoint:generation", "generator")
    assert execution_events[-1] == ("execute", "text", "generator")
    assert provider._local_context_route is not None
    assert provider._local_context_observation is not None
    assert (
        resolve_context_window(
            "generator",
            provider=provider.provider_name,
            local_route=provider._local_context_route,
            local_observation=provider._local_context_observation,
        )
        == 32_768
    )

    service.limits[("endpoint:generation", "generator")] = None
    assert await provider.generate_json("{}") == {"ok": True}
    assert service.events[-1] == ("refresh", "endpoint:generation", "generator")
    assert execution_events[-1] == ("execute", "json", "generator")
    assert provider._local_context_route is not None
    assert provider._local_context_observation is not None
    assert ContextDiagnostic.ENDPOINT_UNAVAILABLE in provider._local_context_observation.diagnostics
    assert (
        resolve_context_window(
            "generator",
            provider=provider.provider_name,
            local_route=provider._local_context_route,
            local_observation=provider._local_context_observation,
        )
        is None
    )

    refresh_count = len(service.events)
    remote_provider = LocalLLMProvider(config, endpoint_name="remote")
    assert await remote_provider.generate_text("remote") == "ok"
    assert len(service.events) == refresh_count
    assert remote_provider._local_context_route is None
    assert remote_provider._local_context_observation is None

    monkeypatch.setattr(
        "gobby.app_context.get_app_context",
        lambda: SimpleNamespace(local_context_service=None),
    )
    provider_without_service = LocalLLMProvider(config, endpoint_name="generation")
    assert await provider_without_service.generate_text("no service") == "ok"
    assert provider_without_service._local_context_route is not None
    assert provider_without_service._local_context_observation is None
    assert (
        resolve_context_window(
            "generator",
            provider=provider_without_service.provider_name,
            local_route=provider_without_service._local_context_route,
            local_observation=provider_without_service._local_context_observation,
        )
        is None
    )

    failed_refresh = AsyncMock(side_effect=RuntimeError("offline"))
    monkeypatch.setattr(
        "gobby.app_context.get_app_context",
        lambda: SimpleNamespace(
            local_context_service=SimpleNamespace(refresh=failed_refresh),
        ),
    )
    provider_after_failure = LocalLLMProvider(config, endpoint_name="generation")
    assert await provider_after_failure.generate_text("failed refresh") == "ok"
    failed_refresh.assert_awaited_once()
    assert provider_after_failure._local_context_route is not None
    assert provider_after_failure._local_context_observation is None

    _install_context(monkeypatch, service)
    manager = WebChatRuntimeManager(codex_client=MagicMock(), daemon_config=config)
    local_session = await manager.create_session(
        provider="codex",
        conversation_id="local",
        model="endpoint:first/same",
    )
    assert isinstance(local_session, CodexManagedChatSession)
    assert local_session._local_context_route is None
    local_refresher = local_session._local_context_refresher
    assert local_refresher is not None
    route, observation = await local_refresher("endpoint:first/same")
    await local_session._set_local_context(route, observation)
    assert service.events[-1] == ("refresh", "endpoint:first", "same")
    assert local_session._resolve_context_window() == 65_536

    refresh_count = len(service.events)
    remote_session = await manager.create_session(
        provider="codex",
        conversation_id="remote",
        model="endpoint:remote/same",
    )
    assert isinstance(remote_session, CodexManagedChatSession)
    assert len(service.events) == refresh_count
    assert remote_session._local_context_route is None
    assert remote_session._local_context_observation is None

    endpoint_shaped_qwen = await manager.create_session(
        provider="qwen",
        conversation_id="qwen",
        model="endpoint:first/same",
    )
    assert isinstance(endpoint_shaped_qwen, QwenManagedChatSession)
    assert len(service.events) == refresh_count
    assert endpoint_shaped_qwen._local_context_route is None
    assert endpoint_shaped_qwen._local_context_refresher is None


@pytest.mark.asyncio
async def test_chat_context_endpoint_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    service = _ContextService(
        {
            ("endpoint:first", "same"): 32_768,
            ("endpoint:first", "other"): 16_384,
            ("endpoint:second", "same"): 65_536,
        }
    )
    _install_context(monkeypatch, service)
    manager = WebChatRuntimeManager(codex_client=MagicMock(), daemon_config=config)

    first = await manager.create_session(
        provider="codex",
        conversation_id="first",
        model="endpoint:first/same",
    )
    second = await manager.create_session(
        provider="codex",
        conversation_id="second",
        model="endpoint:second/same",
    )
    assert isinstance(first, CodexManagedChatSession)
    assert isinstance(second, CodexManagedChatSession)
    first_refresher = first._local_context_refresher
    second_refresher = second._local_context_refresher
    assert first_refresher is not None
    assert second_refresher is not None
    first_context = await first_refresher("endpoint:first/same")
    second_context = await second_refresher("endpoint:second/same")
    await first._set_local_context(*first_context)
    await second._set_local_context(*second_context)

    assert first._resolve_context_window() == 32_768
    assert second._resolve_context_window() == 65_536
    assert first._local_context_route is not None
    assert second._local_context_route is not None
    assert first._local_context_route.endpoint_id == "endpoint:first"
    assert second._local_context_route.endpoint_id == "endpoint:second"

    assert first._local_context_observation is not None
    first_projection = _MessageContextProjection(
        first._local_context_route,
        first._local_context_observation,
    )
    assert first_projection._resolve_context_window_fallback() == 32_768
    assert second._local_context_observation is not None
    second_projection = _MessageContextProjection(
        second._local_context_route,
        second._local_context_observation,
    )
    assert second_projection._resolve_context_window_fallback() == 65_536

    await first.switch_model("endpoint:first/other")

    assert service.events[-1] == ("refresh", "endpoint:first", "other")
    assert first._resolve_context_window() == 16_384
    assert first._local_context_route.model_id == "other"
    assert second._resolve_context_window() == 65_536

    claude = await manager.create_session(
        provider="claude",
        conversation_id="claude",
        model="endpoint:first/same",
    )
    assert isinstance(claude, ChatSession)
    claude_refresher = claude._local_context_refresher
    assert claude_refresher is not None
    claude_context = await claude_refresher("endpoint:first/same")
    await claude._set_local_context(*claude_context)
    assert claude._local_context_route is not None
    assert claude._local_context_route.endpoint_id == "endpoint:first"
    assert claude._resolve_context_window_fallback() == 32_768

    claude._config = config
    claude._client = MagicMock()
    claude._client.set_model = AsyncMock()
    claude._connected = True
    reconnect = AsyncMock()
    monkeypatch.setattr(claude, "_reconnect_for_model_change", reconnect)
    await claude.switch_model("endpoint:second/same")

    reconnect.assert_awaited_once_with("endpoint:second/same")
    claude._client.set_model.assert_not_awaited()
    assert claude._local_context_route is not None
    assert claude._local_context_route.endpoint_id == "endpoint:first"
    assert claude._resolve_context_window_fallback() == 32_768

    refresh_count = len(service.events)
    await claude.switch_model("endpoint:remote/remote-model")
    assert len(service.events) == refresh_count
    assert reconnect.await_count == 2
