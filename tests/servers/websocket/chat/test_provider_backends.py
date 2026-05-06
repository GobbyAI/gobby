"""Focused regression tests for managed provider backend helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.chat.backends.base import ProviderBackendHealth
from gobby.servers.websocket.chat.backends.codex import CodexManagedChatSession
from gobby.servers.websocket.chat.backends.gemini import (
    GeminiManagedChatSession,
    GeminiWebChatBackend,
)
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_dispatch_before_tool_once_shares_inflight_none_response() -> None:
    session = CodexManagedChatSession(conversation_id="conv-codex", _backend=MagicMock())
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_pre_tool(_tool_name: str, _tool_input: dict[str, object]) -> None:
        started.set()
        await release.wait()
        return None

    session._apply_pre_tool_lifecycle = AsyncMock(side_effect=fake_pre_tool)

    first = asyncio.create_task(session._dispatch_before_tool_once("same-tool", "Read", {}))
    await started.wait()
    second = asyncio.create_task(session._dispatch_before_tool_once("same-tool", "Read", {}))
    await drain_asyncio_tasks()

    assert session._apply_pre_tool_lifecycle.await_count == 1

    release.set()
    assert await first is None
    assert await second is None
    assert "same-tool" in session._before_tool_cached_responses

    third = await session._dispatch_before_tool_once("same-tool", "Read", {})
    assert third is None
    assert session._apply_pre_tool_lifecycle.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resume_session_id, expected_request", [(None, "create"), ("prev", "load")]
)
async def test_acp_attach_session_resolves_cwd_and_seeds_trust_before_session_request(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume_session_id: str | None,
    expected_request: str,
) -> None:
    monkeypatch.chdir(temp_dir)
    workspace = temp_dir / "workspace"
    workspace.mkdir()
    expected_cwd = str(workspace.resolve())
    order: list[str] = []

    async def create_session(**_kwargs: object) -> dict[str, str]:
        order.append("create")
        return {"sessionId": "created"}

    async def load_session(_session_id: str, **_kwargs: object) -> dict[str, str]:
        order.append("load")
        return {"sessionId": "loaded"}

    def record_trust(_provider: str, _cwd: str) -> None:
        order.append("trust")

    client = MagicMock()
    client.is_started = True
    client.session_id = None
    client.create_session = AsyncMock(side_effect=create_session)
    client.load_session = AsyncMock(side_effect=load_session)

    backend = GeminiWebChatBackend(client=client, default_model="gemini-default")
    backend._health = ProviderBackendHealth(provider="gemini", available=True)
    session = GeminiManagedChatSession(conversation_id="conv-gemini", _backend=backend)
    session.project_path = "workspace"
    session.resume_session_id = resume_session_id

    with patch(
        "gobby.servers.websocket.chat.backends.acp.pre_approve_directory",
        side_effect=record_trust,
    ) as pre_approve:
        await backend.attach_session(session)

    pre_approve.assert_called_once_with("gemini", expected_cwd)
    assert order == ["trust", expected_request]
    if expected_request == "create":
        client.create_session.assert_awaited_once_with(
            model="gemini-default",
            cwd=expected_cwd,
            reasoning_effort=None,
        )
        client.load_session.assert_not_awaited()
        assert session.sdk_session_id == "created"
    else:
        client.load_session.assert_awaited_once_with(
            "prev",
            model="gemini-default",
            cwd=expected_cwd,
            reasoning_effort=None,
        )
        client.create_session.assert_not_awaited()
        assert session.sdk_session_id == "loaded"
