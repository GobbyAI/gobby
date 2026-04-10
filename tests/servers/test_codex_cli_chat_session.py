"""Tests for CodexCLIChatSession — Codex app-server backed web chat session.

Covers: provider attribute, start (app-server + handshake + thread), send_message
(turn/start + notifications), stop, interrupt.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.codex_cli_chat_session import CodexCLIChatSession

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_process(
    stdout_lines: list[str] | None = None,
    returncode: int | None = None,
) -> AsyncMock:
    """Create a mock asyncio.subprocess.Process."""
    proc = AsyncMock()
    proc.pid = 54321
    proc.returncode = returncode

    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    lines = list(stdout_lines or [])
    line_iter = iter(lines)

    async def _readline() -> bytes:
        try:
            return next(line_iter).encode()
        except StopIteration:
            return b""

    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(side_effect=_readline)
    proc.stderr = MagicMock()

    proc.wait = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    return proc


def _handshake_lines(thread_id: str = "thread-1") -> list[str]:
    """Return stdout lines for initialize + thread/start handshake."""
    return [
        # initialize response
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"userAgent": "Codex/1.0", "codexHome": "/tmp/.codex"},
            }
        )
        + "\n",
        # thread/start response
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "thread": {
                        "id": thread_id,
                        "preview": "",
                        "modelProvider": "openai",
                        "createdAt": 0,
                        "path": "/tmp/codex-session.jsonl",
                    }
                },
            }
        )
        + "\n",
    ]


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
    async def test_start_spawns_app_server_and_handshakes(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        with (
            patch(
                "gobby.servers.codex_cli_chat_session.shutil.which", return_value="/usr/bin/codex"
            ),
            patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc
            ) as mock_exec,
        ):
            session = CodexCLIChatSession(conversation_id="conv-1")
            await session.start()
            assert session.is_connected is True
            assert session._thread_id == "thread-1"
            assert session.sdk_session_id == "thread-1"
            assert session._transcript_path == "/tmp/codex-session.jsonl"

            # Verify spawned with "app-server" subcommand
            call_args = mock_exec.call_args
            assert call_args[0][0] == "/usr/bin/codex"
            assert "app-server" in call_args[0]
            assert call_args.kwargs["env"]["GOBBY_HOOKS_DISABLED"] == "1"

        # Verify initialize and thread/start requests
        assert proc.stdin.write.call_count == 2
        init_req = json.loads(proc.stdin.write.call_args_list[0][0][0].decode())
        assert init_req["method"] == "initialize"
        assert init_req["params"]["clientInfo"]["name"] == "gobby"

        thread_req = json.loads(proc.stdin.write.call_args_list[1][0][0].decode())
        assert thread_req["method"] == "thread/start"

    @pytest.mark.asyncio
    async def test_start_with_model(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        with (
            patch(
                "gobby.servers.codex_cli_chat_session.shutil.which", return_value="/usr/bin/codex"
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        ):
            session = CodexCLIChatSession(conversation_id="conv-1", model="gpt-5.4-mini")
            await session.start()

        thread_req = json.loads(proc.stdin.write.call_args_list[1][0][0].decode())
        assert thread_req["params"]["model"] == "gpt-5.4-mini"

    @pytest.mark.asyncio
    async def test_start_cli_not_found(self) -> None:
        session = CodexCLIChatSession(conversation_id="conv-1")
        with patch("gobby.servers.codex_cli_chat_session.shutil.which", return_value=None):
            with pytest.raises(FileNotFoundError, match="Codex CLI not found"):
                await session.start()

    @pytest.mark.asyncio
    async def test_send_message_streams_text(self) -> None:
        """Verify turn/start request and agent/messageDelta → TextChunk."""
        notification_lines = [
            # turn/start acknowledgment
            json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"turnId": "turn-1"}}) + "\n",
            # Streaming deltas
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "agent/messageDelta",
                    "params": {"delta": "Hello "},
                }
            )
            + "\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "agent/messageDelta",
                    "params": {"delta": "world!"},
                }
            )
            + "\n",
            # Turn completed
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {
                        "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}
                    },
                }
            )
            + "\n",
        ]
        proc = _mock_process(stdout_lines=_handshake_lines() + notification_lines)

        with (
            patch(
                "gobby.servers.codex_cli_chat_session.shutil.which", return_value="/usr/bin/codex"
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        ):
            session = CodexCLIChatSession(conversation_id="conv-1")
            await session.start()
            events = [e async for e in session.send_message("say hello")]

        # Should get 2 TextChunks + 1 DoneEvent
        from gobby.llm.claude_models import DoneEvent, TextChunk

        text_chunks = [e for e in events if isinstance(e, TextChunk)]
        done_events = [e for e in events if isinstance(e, DoneEvent)]

        assert len(text_chunks) == 2
        assert text_chunks[0].content == "Hello "
        assert text_chunks[1].content == "world!"

        assert len(done_events) == 1
        assert done_events[0].input_tokens == 10
        assert done_events[0].output_tokens == 5
        assert done_events[0].cost_usd == pytest.approx(0.001)
        assert done_events[0].sdk_session_id == "thread-1"

        # Verify turn/start request format
        turn_req = json.loads(proc.stdin.write.call_args_list[2][0][0].decode())
        assert turn_req["method"] == "turn/start"
        assert turn_req["params"]["threadId"] == "thread-1"
        assert turn_req["params"]["input"] == [{"type": "text", "text": "say hello"}]

    @pytest.mark.asyncio
    async def test_send_message_falls_back_to_transcript_when_stream_has_no_text(self) -> None:
        """Use transcript text when Codex omits messageDelta notifications."""
        notification_lines = [
            json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"turnId": "turn-1"}}) + "\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {
                        "usage": {"input_tokens": 10, "output_tokens": 0, "cost_usd": 0.001}
                    },
                }
            )
            + "\n",
        ]
        proc = _mock_process(stdout_lines=_handshake_lines() + notification_lines)

        with (
            patch(
                "gobby.servers.codex_cli_chat_session.shutil.which", return_value="/usr/bin/codex"
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        ):
            session = CodexCLIChatSession(conversation_id="conv-1")
            await session.start()
            session._get_transcript_offset = AsyncMock(return_value=123)
            session._get_transcript_assistant_text_since = AsyncMock(
                return_value="live-codex-gpt-5-4"
            )
            events = [e async for e in session.send_message("say hello")]

        from gobby.llm.claude_models import DoneEvent, TextChunk

        text_chunks = [e for e in events if isinstance(e, TextChunk)]
        done_events = [e for e in events if isinstance(e, DoneEvent)]

        assert len(text_chunks) == 1
        assert text_chunks[0].content == "live-codex-gpt-5-4"
        assert len(done_events) == 1
        assert done_events[0].sdk_session_id == "thread-1"
        session._get_transcript_assistant_text_since.assert_awaited_once_with(123)

    @pytest.mark.asyncio
    async def test_send_message_handles_error_response(self) -> None:
        """Error in turn/start response yields TextChunk + DoneEvent."""
        error_lines = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "error": {"code": -32001, "message": "Server overloaded"},
                }
            )
            + "\n",
        ]
        proc = _mock_process(stdout_lines=_handshake_lines() + error_lines)

        with (
            patch(
                "gobby.servers.codex_cli_chat_session.shutil.which", return_value="/usr/bin/codex"
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        ):
            session = CodexCLIChatSession(conversation_id="conv-1")
            await session.start()
            events = [e async for e in session.send_message("test")]

        from gobby.llm.claude_models import DoneEvent, TextChunk

        assert any(isinstance(e, TextChunk) and "Server overloaded" in e.content for e in events)
        assert any(isinstance(e, DoneEvent) for e in events)

    @pytest.mark.asyncio
    async def test_stop_terminates(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        with (
            patch(
                "gobby.servers.codex_cli_chat_session.shutil.which", return_value="/usr/bin/codex"
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        ):
            session = CodexCLIChatSession(conversation_id="conv-1")
            await session.start()
            assert session.is_connected is True

            await session.stop()
            assert session.is_connected is False
            assert session._thread_id is None
            proc.terminate.assert_called_once()

    def test_switch_model(self) -> None:
        session = CodexCLIChatSession(conversation_id="conv-1", model="gpt-4")
        assert session.model == "gpt-4"

    @pytest.mark.asyncio
    async def test_interrupt_sends_interrupt(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        with (
            patch(
                "gobby.servers.codex_cli_chat_session.shutil.which", return_value="/usr/bin/codex"
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc),
        ):
            session = CodexCLIChatSession(conversation_id="conv-1")
            await session.start()
            await session.interrupt()

        interrupt_req = json.loads(proc.stdin.write.call_args_list[-1][0][0].decode())
        assert interrupt_req["method"] == "turn/interrupt"
        assert interrupt_req["params"]["threadId"] == "thread-1"

    @pytest.mark.asyncio
    async def test_interrupt_noop_when_no_process(self) -> None:
        session = CodexCLIChatSession(conversation_id="conv-1")
        await session.interrupt()  # Should not raise
