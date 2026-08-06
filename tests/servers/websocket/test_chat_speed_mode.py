from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.providers.capabilities.models import ActivationDescriptor, SpeedMode
from gobby.providers.capabilities.resolve import SpeedResolution, SpeedStatus
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.chat_stream_transport import ChatStreamTransport
from gobby.servers.websocket.chat._message_ingress import ChatMessageIngressMixin
from gobby.servers.websocket.chat._streaming import ChatStreamingMixin

pytestmark = pytest.mark.unit


class _Ingress(ChatMessageIngressMixin):
    def __init__(self) -> None:
        self.clients: dict[object, dict[str, object]] = {}
        self._active_chat_tasks: dict[str, asyncio.Task[None]] = {}
        self.speed_modes: list[str] = []
        self.errors: list[str] = []

    async def _cancel_active_chat(self, conversation_id: str) -> None:
        del conversation_id

    def _get_session_create_lock(self, conversation_id: str) -> asyncio.Lock:
        del conversation_id
        return asyncio.Lock()

    def _apply_tts_intent(self, conversation_id: str, tts_enabled: object) -> None:
        del conversation_id, tts_enabled

    async def _send_error(self, *args: object, **kwargs: object) -> None:
        del kwargs
        self.errors.append(str(args[1]))

    def _build_inject_context(self, *args: object) -> None:
        del args

    async def _stream_chat_response(
        self,
        *args: object,
        speed_mode: str = "standard",
        **kwargs: object,
    ) -> None:
        del args, kwargs
        self.speed_modes.append(speed_mode)

    def _on_chat_task_done(self, task: asyncio.Task[None]) -> None:
        task.result()


class _Resolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, SpeedMode, str]] = []

    def resolve_route(
        self,
        provider: str,
        model: str,
        speed_mode: SpeedMode,
        surface: str,
    ) -> SpeedResolution:
        self.calls.append((provider, model, speed_mode, surface))
        return SpeedResolution(
            requested=speed_mode,
            effective=SpeedMode.FAST,
            status=SpeedStatus.FAST_CONFIGURED,
            selector="model-fast",
            activations=(
                ActivationDescriptor("model_selector", "tool-chat", {}),
                ActivationDescriptor(
                    "request_parameter",
                    "tool-chat",
                    {"name": "serviceTier", "value": "priority"},
                ),
            ),
            reason=None,
        )


class _FailingChatSession:
    def __init__(self) -> None:
        self.model: str | None = "model-standard"
        self.provider = "claude"
        self.project_id: str | None = None
        self.db_session_id: str | None = None
        self.switch_calls: list[str] = []
        self.requests: list[tuple[object, dict[str, object], str | None]] = []
        self._tool_approval_callback: Any = None

    async def switch_model(self, model: str) -> None:
        self.switch_calls.append(model)
        self.model = model

    def send_message(
        self,
        content: object,
        *,
        request_parameters: Mapping[str, object] | None = None,
    ) -> AsyncIterator[object]:
        self.requests.append((content, dict(request_parameters or {}), self.model))

        async def failing_stream() -> AsyncIterator[object]:
            raise RuntimeError("dispatch failed")
            yield object()

        return failing_stream()


class _Transport:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def base_msg(self, **fields: Any) -> dict[str, Any]:
        return dict(fields)

    async def send_direct(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


class _Streaming(ChatStreamingMixin):
    def __init__(self, session: _FailingChatSession) -> None:
        self._chat_sessions = {
            "conversation": cast(ChatSessionProtocol, session),
        }
        self._active_chat_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_inject_contexts: dict[str, str] = {}
        self.session_manager: object | None = None


@pytest.mark.asyncio
async def test_per_send_not_sticky(monkeypatch: pytest.MonkeyPatch) -> None:
    async def prepare(*args: Any, **kwargs: Any) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(records=())

    monkeypatch.setattr(
        "gobby.servers.websocket.chat._message_ingress.prepare_chat_attachments_or_error",
        prepare,
    )
    ingress = _Ingress()
    websocket = object()
    ingress.clients[websocket] = {"type": "web"}

    await ingress._handle_chat_message(
        websocket,
        {"content": "first", "conversation_id": "conversation", "speed_mode": "fast"},
    )
    await ingress._active_chat_tasks["conversation"]
    await ingress._handle_chat_message(
        websocket,
        {"content": "second", "conversation_id": "conversation"},
    )
    await ingress._active_chat_tasks["conversation"]

    assert ingress.speed_modes == ["fast", "standard"]
    assert ingress.errors == []


@pytest.mark.asyncio
async def test_unhashable_speed_mode_is_rejected() -> None:
    ingress = _Ingress()
    websocket = object()
    ingress.clients[websocket] = {"type": "web"}

    await ingress._handle_chat_message(
        websocket,
        {"content": "hello", "speed_mode": ["fast"]},
    )

    assert ingress.errors == ["Invalid speed_mode '['fast']'"]


@pytest.mark.asyncio
async def test_fast_dispatch_restores_model_when_stream_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _Resolver()
    monkeypatch.setattr(
        "gobby.app_context.get_app_context",
        lambda: SimpleNamespace(provider_capability_resolver=resolver),
    )
    session = _FailingChatSession()
    transport = _Transport()
    streaming = _Streaming(session)

    await streaming._run_chat_turn(
        conversation_id="conversation",
        content="hello",
        model=None,
        transport=cast(ChatStreamTransport, transport),
        speed_mode="fast",
        tts_enabled=False,
    )

    assert resolver.calls == [
        ("claude", "model-standard", SpeedMode.FAST, "tool-chat"),
    ]
    assert session.requests == [
        ("hello", {"serviceTier": "priority"}, "model-fast"),
    ]
    assert session.switch_calls == ["model-fast", "model-standard"]
    assert session.model == "model-standard"
    assert [message["type"] for message in transport.messages] == ["chat_error"]
