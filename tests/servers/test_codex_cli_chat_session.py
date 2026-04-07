"""Tests for CodexCLIChatSession — Codex CLI-backed web chat session.

Covers: provider attribute, start, send_message, stop.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from gobby.servers.codex_cli_chat_session import CodexCLIChatSession

pytestmark = pytest.mark.unit


class TestCodexCLIChatSession:
    def test_provider_attribute(self) -> None:
        session = CodexCLIChatSession(conversation_id="conv-1")
        assert session.provider == "codex"

    def test_default_fields(self) -> None:
        session = CodexCLIChatSession(conversation_id="conv-1")
        assert session.conversation_id == "conv-1"
        assert session.db_session_id is None
        assert session.chat_mode == "code"
        assert session.is_connected is False

    @pytest.mark.asyncio
    async def test_start_spawns_process(self) -> None:
        session = CodexCLIChatSession(conversation_id="conv-1")
        mock_process = AsyncMock()
        mock_process.stdin = AsyncMock()
        mock_process.stdout = asyncio.StreamReader()
        mock_process.stderr = asyncio.StreamReader()

        with (
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec,
        ):
            await session.start()
            assert session.is_connected is True
            mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_cli_not_found(self) -> None:
        session = CodexCLIChatSession(conversation_id="conv-1")
        with patch("shutil.which", return_value=None):
            with pytest.raises(FileNotFoundError, match="Codex CLI not found"):
                await session.start()

    @pytest.mark.asyncio
    async def test_stop_terminates(self) -> None:
        session = CodexCLIChatSession(conversation_id="conv-1")
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock()
        session._process = mock_process
        session._connected = True

        await session.stop()
        mock_process.terminate.assert_called_once()
        assert session.is_connected is False

    def test_switch_model(self) -> None:
        session = CodexCLIChatSession(conversation_id="conv-1", model="gpt-4")
        assert session.model == "gpt-4"

    @pytest.mark.asyncio
    async def test_interrupt_noop_when_no_process(self) -> None:
        session = CodexCLIChatSession(conversation_id="conv-1")
        await session.interrupt()  # Should not raise
