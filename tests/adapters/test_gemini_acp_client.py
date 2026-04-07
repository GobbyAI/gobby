"""Tests for GeminiACPClient -- Gemini ACP subprocess wrapper.

Covers:
- Construction and defaults
- start (subprocess launch, resume, CLI not found)
- send (JSON-RPC request, NDJSON stream parsing, event normalization)
- stop (terminate, cleanup)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.gemini_acp_client import GeminiACPClient

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_process(
    stdout_lines: list[str] | None = None,
    returncode: int | None = None,
) -> AsyncMock:
    """Create a mock asyncio.subprocess.Process.

    Args:
        stdout_lines: Lines to return from stdout.readline().
        returncode: Process return code (None = still running).
    """
    proc = AsyncMock()
    proc.pid = 12345
    proc.returncode = returncode

    # stdin
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    # stdout -- queue-based readline
    lines = list(stdout_lines or [])
    line_iter = iter(lines)

    async def _readline() -> bytes:
        try:
            return next(line_iter).encode()
        except StopIteration:
            return b""

    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(side_effect=_readline)

    # stderr
    proc.stderr = MagicMock()

    # wait / terminate / kill
    proc.wait = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    return proc


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_state(self) -> None:
        client = GeminiACPClient()
        assert not client.is_started
        assert client._cli_path is None

    def test_custom_cli_path(self) -> None:
        client = GeminiACPClient(cli_path="/usr/local/bin/gemini")
        assert client._cli_path == "/usr/local/bin/gemini"


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


class TestStart:
    @pytest.mark.asyncio
    async def test_start_launches_subprocess(self) -> None:
        proc = _mock_process()
        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as mock_exec:
                client = GeminiACPClient()
                await client.start()

                assert client.is_started
                mock_exec.assert_awaited_once()
                # Verify command includes --acp
                call_args = mock_exec.call_args
                assert call_args[0][0] == "/usr/bin/gemini"
                assert "--acp" in call_args[0]

    @pytest.mark.asyncio
    async def test_start_with_resume_session(self) -> None:
        proc = _mock_process()
        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as mock_exec:
                client = GeminiACPClient()
                await client.start(session_id="prev-session-123")

                call_args = mock_exec.call_args
                assert "--resume" in call_args[0]
                assert "prev-session-123" in call_args[0]

    @pytest.mark.asyncio
    async def test_start_raises_when_cli_not_found(self) -> None:
        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value=None):
            client = GeminiACPClient()
            with pytest.raises(FileNotFoundError, match="Gemini CLI not found"):
                await client.start()

    @pytest.mark.asyncio
    async def test_start_raises_when_already_started(self) -> None:
        proc = _mock_process()
        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                with pytest.raises(RuntimeError, match="already started"):
                    await client.start()

    @pytest.mark.asyncio
    async def test_start_uses_custom_cli_path(self) -> None:
        proc = _mock_process()
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ) as mock_exec:
            client = GeminiACPClient(cli_path="/custom/gemini")
            await client.start()

            call_args = mock_exec.call_args
            assert call_args[0][0] == "/custom/gemini"


# ---------------------------------------------------------------------------
# Send -- stream parsing
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_send_writes_jsonrpc_request(self) -> None:
        result_line = json.dumps({"type": "result", "stats": {}}) + "\n"
        proc = _mock_process(stdout_lines=[result_line])

        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                events = [e async for e in client.send("hello")]

        # Verify stdin write
        proc.stdin.write.assert_called_once()
        written = proc.stdin.write.call_args[0][0].decode()
        parsed = json.loads(written)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["method"] == "send"
        assert parsed["params"]["message"] == "hello"

        # Should have a result event
        assert len(events) == 1
        assert events[0].event_type == "result"

    @pytest.mark.asyncio
    async def test_send_yields_init_event(self) -> None:
        lines = [
            json.dumps({"type": "init", "session_id": "s1"}) + "\n",
            json.dumps({"type": "result", "stats": {}}) + "\n",
        ]
        proc = _mock_process(stdout_lines=lines)

        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                events = [e async for e in client.send("hi")]

        assert len(events) == 2
        assert events[0].event_type == "init"
        assert events[0].data["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_send_yields_content_delta(self) -> None:
        lines = [
            json.dumps({"type": "message", "role": "assistant", "delta": True, "content": "Hello "})
            + "\n",
            json.dumps({"type": "message", "role": "assistant", "delta": True, "content": "world!"})
            + "\n",
            json.dumps({"type": "result", "stats": {"tokens": 10}}) + "\n",
        ]
        proc = _mock_process(stdout_lines=lines)

        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                events = [e async for e in client.send("say hello")]

        deltas = [e for e in events if e.event_type == "content_delta"]
        assert len(deltas) == 2
        assert deltas[0].data["content"] == "Hello "
        assert deltas[1].data["content"] == "world!"

    @pytest.mark.asyncio
    async def test_send_yields_error_event(self) -> None:
        lines = [
            json.dumps({"type": "error", "message": "rate limited"}) + "\n",
        ]
        proc = _mock_process(stdout_lines=lines)

        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                events = [e async for e in client.send("oops")]

        assert len(events) == 1
        assert events[0].event_type == "error"
        assert events[0].data["message"] == "rate limited"

    @pytest.mark.asyncio
    async def test_send_raises_when_not_started(self) -> None:
        client = GeminiACPClient()
        with pytest.raises(RuntimeError, match="not started"):
            async for _ in client.send("hi"):
                pass

    @pytest.mark.asyncio
    async def test_send_raises_when_process_exited(self) -> None:
        proc = _mock_process(returncode=1)

        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                # Simulate process exit
                proc.returncode = 1
                with pytest.raises(RuntimeError, match="has exited"):
                    async for _ in client.send("hi"):
                        pass

    @pytest.mark.asyncio
    async def test_send_skips_non_json_lines(self) -> None:
        lines = [
            "not json at all\n",
            json.dumps({"type": "result", "stats": {}}) + "\n",
        ]
        proc = _mock_process(stdout_lines=lines)

        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                events = [e async for e in client.send("test")]

        # Only the result event should be yielded
        assert len(events) == 1
        assert events[0].event_type == "result"


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_terminates_process(self) -> None:
        proc = _mock_process()

        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                assert client.is_started

                await client.stop()

        assert not client.is_started
        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_when_not_started_is_noop(self) -> None:
        client = GeminiACPClient()
        await client.stop()  # Should not raise
        assert not client.is_started

    @pytest.mark.asyncio
    async def test_stop_when_process_already_exited(self) -> None:
        proc = _mock_process(returncode=0)

        with patch("gobby.adapters.gemini_acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                proc.returncode = 0  # Already exited
                await client.stop()

        assert not client.is_started
        proc.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------


class TestNormalizeEvent:
    def test_init_event(self) -> None:
        event = GeminiACPClient._normalize_event(
            {"type": "init", "session_id": "abc", "version": "1.0"}
        )
        assert event.event_type == "init"
        assert event.data["session_id"] == "abc"
        assert "type" not in event.data

    def test_content_delta(self) -> None:
        event = GeminiACPClient._normalize_event(
            {"type": "message", "role": "assistant", "delta": True, "content": "hi"}
        )
        assert event.event_type == "content_delta"
        assert event.data["content"] == "hi"

    def test_non_delta_message(self) -> None:
        event = GeminiACPClient._normalize_event(
            {"type": "message", "role": "assistant", "content": "full response"}
        )
        assert event.event_type == "message"
        assert event.data["content"] == "full response"

    def test_result_event(self) -> None:
        event = GeminiACPClient._normalize_event({"type": "result", "stats": {"tokens": 42}})
        assert event.event_type == "result"
        assert event.data["stats"]["tokens"] == 42

    def test_error_event(self) -> None:
        event = GeminiACPClient._normalize_event(
            {"type": "error", "message": "bad request", "code": 400}
        )
        assert event.event_type == "error"
        assert event.data["message"] == "bad request"
        assert event.data["code"] == 400

    def test_unknown_event_passes_through(self) -> None:
        event = GeminiACPClient._normalize_event({"type": "custom", "foo": "bar"})
        assert event.event_type == "custom"
        assert event.data["foo"] == "bar"
