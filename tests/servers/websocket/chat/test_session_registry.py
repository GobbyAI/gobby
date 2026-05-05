"""Tests for the shared web-chat session registry."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from gobby.llm.claude_models import DoneEvent
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry

pytestmark = pytest.mark.unit


async def _done_stream():
    yield DoneEvent(tool_calls_count=0)


class TestWebChatSessionRegistry:
    def test_lookup_by_conversation_id_and_db_session_id(self) -> None:
        registry = WebChatSessionRegistry()
        session = MagicMock()
        session.db_session_id = "db-id"
        session.conversation_id = "conv-1"

        registry.register("conv-1", session)

        assert registry.find_session("conv-1") == ("conv-1", session)
        assert registry.find_session("db-id") == ("conv-1", session)
        assert registry.find_session("missing") == (None, None)

    @pytest.mark.asyncio
    async def test_compact_session_drains_until_done_event(self) -> None:
        registry = WebChatSessionRegistry()
        session = MagicMock()
        session.db_session_id = "db-id"
        session.send_message.side_effect = lambda command: _done_stream()
        registry.register("conv-1", session)

        result = await registry.compact_session("db-id")

        assert result == {
            "compacted": True,
            "command": "/compact",
            "via": "web_chat",
            "queued": False,
        }
        assert [call.args[0] for call in session.send_message.call_args_list] == [
            "/compact",
            "Continue where you last left off.",
        ]

    @pytest.mark.asyncio
    async def test_active_session_queues_compaction_until_turn_completes(self) -> None:
        registry = WebChatSessionRegistry()
        session = MagicMock()
        session.db_session_id = "db-id"
        session.send_message.side_effect = lambda command: _done_stream()
        registry.register("conv-1", session)

        release = asyncio.Event()

        async def active_turn() -> None:
            await release.wait()

        active_task = asyncio.create_task(active_turn())
        registry.track_active_task("conv-1", active_task)

        result = await registry.compact_session("db-id")

        assert result["compacted"] is True
        assert result["queued"] is True
        session.send_message.assert_not_called()

        release.set()
        await active_task
        await asyncio.sleep(0)
        queued_task = registry._queued_compaction_tasks.get("conv-1")
        assert queued_task is not None
        await queued_task
        assert [call.args[0] for call in session.send_message.call_args_list] == [
            "/compact",
            "Continue where you last left off.",
        ]

    @pytest.mark.asyncio
    async def test_drain_failure_returns_compacted_false(self) -> None:
        registry = WebChatSessionRegistry()
        session = MagicMock()
        session.db_session_id = "db-id"

        async def broken_stream():
            raise RuntimeError("boom")
            yield DoneEvent(tool_calls_count=0)

        session.send_message.side_effect = lambda command: broken_stream()
        registry.register("conv-1", session)

        result = await registry.compact_session("db-id")

        assert result["compacted"] is False
        assert "boom" in result["reason"]
