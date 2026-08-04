from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.servers.websocket.chat._message_ingress import ChatMessageIngressMixin

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
