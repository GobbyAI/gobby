"""Codex web-chat context-clear (real thread rotation) for plan approval (#15637).

Codex's "approve + clear context" plan option must perform a *real* context
reset, not a stub: archive the current thread and start a fresh one. The plan
handler re-seeds the approved plan into the next turn so implementation
continues on a clean thread.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.servers.websocket.chat.backends.base import ProviderBackendHealth
from gobby.servers.websocket.chat.backends.codex import (
    CodexManagedChatSession,
    CodexWebChatBackend,
)

pytestmark = pytest.mark.unit


def _backend_with_fake_client() -> tuple[CodexWebChatBackend, SimpleNamespace]:
    fake_client = SimpleNamespace(archive_thread=AsyncMock(), is_connected=True)
    backend = CodexWebChatBackend(client=fake_client)  # type: ignore[arg-type]
    backend._health = ProviderBackendHealth(provider="codex", available=True)
    return backend, fake_client


@pytest.mark.asyncio
async def test_clear_context_archives_old_thread_and_reattaches() -> None:
    backend, fake_client = _backend_with_fake_client()
    session = CodexManagedChatSession(conversation_id="c", _backend=backend)
    session._model = "gpt-5.6-sol"
    session._model_selector = "endpoint:openrouter"
    session._thread_id = "old-thread"
    backend._sessions_by_thread["old-thread"] = session

    reattach_calls: list[str | None] = []

    async def fake_attach(sess: CodexManagedChatSession, *, model: str | None = None) -> None:
        sess._thread_id = "new-thread"
        sess._connected = True
        reattach_calls.append(model)

    backend.attach_session = fake_attach  # type: ignore[method-assign]

    result = await session.clear_context()

    assert result is True
    fake_client.archive_thread.assert_awaited_once_with("old-thread")
    # The stale thread is detached before a fresh one is attached.
    assert "old-thread" not in backend._sessions_by_thread
    assert session._thread_id == "new-thread"
    assert reattach_calls == ["endpoint:openrouter"]


@pytest.mark.asyncio
async def test_clear_context_does_not_resume_prior_thread() -> None:
    fake_client = SimpleNamespace(
        archive_thread=AsyncMock(),
        start_thread=AsyncMock(return_value=SimpleNamespace(id="new-thread", path=None)),
        resume_thread=AsyncMock(return_value=SimpleNamespace(id="resumed-thread", path=None)),
        is_connected=True,
    )
    backend = CodexWebChatBackend(client=fake_client)  # type: ignore[arg-type]
    backend._health = ProviderBackendHealth(provider="codex", available=True)
    session = CodexManagedChatSession(
        conversation_id="c",
        _backend=backend,
        sandbox_config=SandboxConfig(enabled=False),
    )
    session._model = "gpt-5.6-sol"
    session.chat_mode = "normal"
    session._thread_id = "old-thread"
    session.resume_session_id = "resume-me"
    session.sdk_session_id = "old-thread"
    backend._sessions_by_thread["old-thread"] = session

    result = await session.clear_context()

    assert result is True
    fake_client.archive_thread.assert_awaited_once_with("old-thread")
    fake_client.resume_thread.assert_not_awaited()
    fake_client.start_thread.assert_awaited_once()
    assert session._thread_id == "new-thread"
    assert session.sdk_session_id == "new-thread"
    assert session.resume_session_id is None
    assert session.chat_mode == "normal"
    assert session._model == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_clear_context_unavailable_backend_is_noop() -> None:
    backend, fake_client = _backend_with_fake_client()
    backend._health = ProviderBackendHealth(provider="codex", available=False)
    session = CodexManagedChatSession(conversation_id="c", _backend=backend)
    session._thread_id = "old-thread"

    result = await session.clear_context()

    assert result is False
    fake_client.archive_thread.assert_not_awaited()
    # The thread is left intact when the backend cannot service the reset.
    assert session._thread_id == "old-thread"
