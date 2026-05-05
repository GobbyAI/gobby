"""Focused regression tests for managed provider backend helpers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.servers.websocket.chat.backends.codex import CodexManagedChatSession
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
