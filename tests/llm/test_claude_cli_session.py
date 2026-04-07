"""Tests for ClaudeCLI session launcher — subprocess management for multi-turn web chat.

Covers: start, send+stream, interrupt, stop, CLI not found, env overrides.
"""

from __future__ import annotations

import asyncio
import json
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.llm.claude_cli import ClaudeCLI, CLISession

pytestmark = pytest.mark.unit


# ─── ClaudeCLI tests ──────────────────────────────────────────────────


class TestClaudeCLI:
    def test_session_returns_cli_session(self) -> None:
        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session()
        assert isinstance(session, CLISession)

    def test_session_with_model(self) -> None:
        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session(model="claude-sonnet-4-6")
        assert session._model == "claude-sonnet-4-6"

    def test_session_with_session_id(self) -> None:
        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session(session_id="sess-123")
        assert session._session_id == "sess-123"

    def test_env_overrides_allowed(self) -> None:
        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session(env_overrides={"ANTHROPIC_API_KEY": "sk-test"})
        assert session._env_overrides == {"ANTHROPIC_API_KEY": "sk-test"}

    def test_env_overrides_disallowed_key_raises(self) -> None:
        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        with pytest.raises(ValueError, match="Disallowed env overrides"):
            cli.session(env_overrides={"PATH": "/evil"})

    def test_env_overrides_ssrf_prevention(self) -> None:
        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        with pytest.raises(ValueError, match="ANTHROPIC_BASE_URL must be localhost"):
            cli.session(env_overrides={"ANTHROPIC_BASE_URL": "https://evil.com/api"})

    def test_env_overrides_localhost_allowed(self) -> None:
        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session(env_overrides={"ANTHROPIC_BASE_URL": "http://localhost:8080"})
        assert session._env_overrides["ANTHROPIC_BASE_URL"] == "http://localhost:8080"

    def test_env_overrides_127_allowed(self) -> None:
        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session(env_overrides={"ANTHROPIC_BASE_URL": "http://127.0.0.1:8080"})
        assert "ANTHROPIC_BASE_URL" in session._env_overrides


# ─── CLISession tests ─────────────────────────────────────────────────


class TestCLISession:
    @pytest.mark.asyncio
    async def test_start_spawns_subprocess(self) -> None:
        mock_process = AsyncMock()
        mock_process.stdin = AsyncMock()
        mock_process.stdout = asyncio.StreamReader()
        mock_process.stderr = asyncio.StreamReader()

        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await session.start()
            mock_exec.assert_called_once()
            cmd = mock_exec.call_args[0]
            assert cmd[0] == "/usr/bin/claude"
            assert "--output-format" in cmd
            assert "stream-json" in cmd
            assert "--input-format" in cmd

    @pytest.mark.asyncio
    async def test_start_includes_session_id(self) -> None:
        mock_process = AsyncMock()
        mock_process.stdin = AsyncMock()
        mock_process.stdout = asyncio.StreamReader()
        mock_process.stderr = asyncio.StreamReader()

        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session(session_id="sess-abc")

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await session.start()
            cmd = mock_exec.call_args[0]
            assert "--session-id" in cmd
            idx = cmd.index("--session-id")
            assert cmd[idx + 1] == "sess-abc"

    @pytest.mark.asyncio
    async def test_start_includes_model(self) -> None:
        mock_process = AsyncMock()
        mock_process.stdin = AsyncMock()
        mock_process.stdout = asyncio.StreamReader()
        mock_process.stderr = asyncio.StreamReader()

        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session(model="claude-opus-4-6")

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await session.start()
            cmd = mock_exec.call_args[0]
            assert "--model" in cmd
            idx = cmd.index("--model")
            assert cmd[idx + 1] == "claude-opus-4-6"

    @pytest.mark.asyncio
    async def test_start_cli_not_found(self) -> None:
        cli = ClaudeCLI()
        session = cli.session()

        with patch("gobby.llm.claude_cli.find_cli_path", return_value=None):
            with pytest.raises(FileNotFoundError, match="Claude CLI not found"):
                await session.start()

    @pytest.mark.asyncio
    async def test_send_writes_to_stdin_and_streams(self) -> None:
        mock_process = AsyncMock()
        mock_stdin = AsyncMock()
        mock_process.stdin = mock_stdin

        # Set up stdout with a result event
        stdout_reader = asyncio.StreamReader()
        result_line = json.dumps(
            {
                "type": "result",
                "cost_usd": 0.001,
                "input_tokens": 10,
                "output_tokens": 5,
                "session_id": "s1",
            }
        )
        stdout_reader.feed_data((result_line + "\n").encode())
        stdout_reader.feed_eof()
        mock_process.stdout = stdout_reader
        mock_process.stderr = asyncio.StreamReader()

        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await session.start()

        from gobby.llm.stream_json_parser import ResultEvent

        events = []
        async for event in session.send("Hello"):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)
        # Verify stdin was written to
        mock_stdin.write.assert_called_once()
        written = mock_stdin.write.call_args[0][0]
        payload = json.loads(written.decode())
        assert payload["type"] == "user"
        assert payload["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_interrupt_sends_sigint(self) -> None:
        mock_process = MagicMock()
        mock_process.stdin = AsyncMock()
        mock_process.stdout = asyncio.StreamReader()
        mock_process.stderr = asyncio.StreamReader()

        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await session.start()

        await session.interrupt()
        mock_process.send_signal.assert_called_once_with(signal.SIGINT)

    @pytest.mark.asyncio
    async def test_stop_terminates_process(self) -> None:
        mock_process = AsyncMock()
        mock_process.stdin = AsyncMock()
        mock_process.stdout = asyncio.StreamReader()
        mock_process.stderr = asyncio.StreamReader()
        mock_process.wait = AsyncMock()

        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            await session.start()

        await session.stop()
        mock_process.terminate.assert_called_once()
        mock_process.wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_interrupt_noop_when_no_process(self) -> None:
        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session()
        # Should not raise
        await session.interrupt()

    @pytest.mark.asyncio
    async def test_stop_noop_when_no_process(self) -> None:
        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session()
        # Should not raise
        await session.stop()

    @pytest.mark.asyncio
    async def test_env_overrides_passed_to_subprocess(self) -> None:
        mock_process = AsyncMock()
        mock_process.stdin = AsyncMock()
        mock_process.stdout = asyncio.StreamReader()
        mock_process.stderr = asyncio.StreamReader()

        cli = ClaudeCLI(cli_path="/usr/bin/claude")
        session = cli.session(env_overrides={"ANTHROPIC_API_KEY": "sk-test-123"})

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await session.start()
            call_kwargs = mock_exec.call_args[1]
            env = call_kwargs["env"]
            assert env["ANTHROPIC_API_KEY"] == "sk-test-123"
