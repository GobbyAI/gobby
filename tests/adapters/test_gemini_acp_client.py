"""Tests for GeminiACPClient -- Gemini ACP subprocess wrapper.

Covers:
- Construction and defaults
- start (subprocess launch, initialize handshake, session creation)
- send (JSON-RPC session/prompt request, notification parsing, event normalization)
- stop (terminate, cleanup)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.gemini_acp_client import (
    DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS,
    GeminiACPClient,
)
from tests._timing import wait_forever

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_process(
    stdout_lines: list[str] | None = None,
    returncode: int | None = None,
    stderr_text: str = "",
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
    proc.stderr.read = AsyncMock(return_value=stderr_text.encode())

    # wait / terminate / kill
    proc.wait = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    return proc


def _recording_wait_for(
    calls: list[float],
) -> Callable[[Awaitable[object], float], Awaitable[object]]:
    async def wait_for(awaitable: Awaitable[object], timeout: float) -> object:
        calls.append(timeout)
        return await awaitable

    return wait_for


def _handshake_lines(session_id: str = "sess-1") -> list[str]:
    """Return the stdout lines for a successful initialize + session/new handshake."""
    return [
        # initialize response
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}) + "\n",
        # session/new response
        json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": session_id}}) + "\n",
    ]


def _resume_handshake_lines(session_id: str = "prev-123") -> list[str]:
    """Return the stdout lines for initialize + session/load handshake."""
    return [
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}) + "\n",
        json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": session_id}}) + "\n",
    ]


def _resume_handshake_lines_without_session_id() -> list[str]:
    """Return stdout lines for session/load where ACP responds with null result."""
    return [
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}) + "\n",
        json.dumps({"jsonrpc": "2.0", "id": 2, "result": None}) + "\n",
    ]


def _initialize_only_lines() -> list[str]:
    """Return stdout lines for initialize-only startup."""
    return [
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}) + "\n",
    ]


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_state(self) -> None:
        client = GeminiACPClient()
        assert not client.is_started
        assert client._cli_path is None
        assert client.session_id is None
        assert client._purpose == "runtime"
        assert client._prompt_timeout == DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS

    def test_custom_cli_path(self) -> None:
        client = GeminiACPClient(cli_path="/usr/local/bin/gemini")
        assert client._cli_path == "/usr/local/bin/gemini"

    def test_prompt_timeout_can_be_overridden_by_env(self) -> None:
        with patch.dict(
            "os.environ",
            {"GOBBY_GEMINI_ACP_PROMPT_TIMEOUT_SECONDS": "75"},
            clear=False,
        ):
            client = GeminiACPClient()

        assert client._prompt_timeout == 75.0

    def test_invalid_prompt_timeout_env_falls_back_to_default(self) -> None:
        with patch.dict(
            "os.environ",
            {"GOBBY_GEMINI_ACP_PROMPT_TIMEOUT_SECONDS": "not-a-number"},
            clear=False,
        ):
            client = GeminiACPClient()

        assert client._prompt_timeout == DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


class TestStart:
    @pytest.mark.asyncio
    async def test_start_launches_subprocess_and_handshakes(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as mock_exec:
                client = GeminiACPClient()
                await client.start()

                assert client.is_started
                assert client.session_id == "sess-1"
                mock_exec.assert_awaited_once()
                # Verify command includes --acp
                call_args = mock_exec.call_args
                assert call_args[0][0] == "/usr/bin/gemini"
                assert "--acp" in call_args[0]

    @pytest.mark.asyncio
    async def test_start_stores_session_info(self) -> None:
        proc = _mock_process(
            stdout_lines=[
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}) + "\n",
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "sessionId": "sess-1",
                            "models": {
                                "availableModels": [
                                    {"modelId": "gemini-3.1-pro-preview", "name": "Pro"}
                                ]
                            },
                        },
                    }
                )
                + "\n",
            ]
        )
        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()

        assert client.session_info["sessionId"] == "sess-1"
        assert client.session_info["models"]["availableModels"][0]["modelId"] == (
            "gemini-3.1-pro-preview"
        )

    @pytest.mark.asyncio
    async def test_start_with_model_includes_cli_flag(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as mock_exec:
                client = GeminiACPClient()
                await client.start(model="gemini-3.1-pro-preview")

                call_args = mock_exec.call_args
                assert call_args[0] == (
                    "/usr/bin/gemini",
                    "--acp",
                    "--model",
                    "gemini-3.1-pro-preview",
                )
                assert call_args.kwargs["env"]["GOBBY_HOOKS_DISABLED"] == "1"
                assert call_args.kwargs["env"]["GEMINI_CLI_NO_RELAUNCH"] == "true"

        # Verify initialize and session/new requests were sent
        assert proc.stdin.write.call_count == 2
        init_req = json.loads(proc.stdin.write.call_args_list[0][0][0].decode())
        assert init_req["method"] == "initialize"
        assert init_req["params"]["protocolVersion"] == 1
        assert init_req["params"]["clientInfo"]["name"] == "gobby"
        assert init_req["params"]["clientCapabilities"] == {}
        assert "capabilities" not in init_req["params"]

        session_req = json.loads(proc.stdin.write.call_args_list[1][0][0].decode())
        assert session_req["method"] == "session/new"
        assert session_req["params"]["cwd"] == "."
        assert session_req["params"]["mcpServers"] == []

    @pytest.mark.asyncio
    async def test_start_keeps_required_env_even_with_env_overrides(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ) as mock_exec:
                client = GeminiACPClient(
                    env_overrides={
                        "GOBBY_HOOKS_DISABLED": "0",
                        "GEMINI_CLI_NO_RELAUNCH": "false",
                    }
                )
                await client.start()

                env = mock_exec.call_args.kwargs["env"]
                assert env["GOBBY_HOOKS_DISABLED"] == "1"
                assert env["GOBBY_ACP_CHILD"] == "1"
                assert env["GEMINI_CLI_NO_RELAUNCH"] == "true"

    @pytest.mark.asyncio
    async def test_start_with_resume_uses_load_session(self) -> None:
        proc = _mock_process(stdout_lines=_resume_handshake_lines("prev-123"))
        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start(session_id="prev-123")

                assert client.session_id == "prev-123"

        # Verify session/load (not session/new) was called
        session_req = json.loads(proc.stdin.write.call_args_list[1][0][0].decode())
        assert session_req["method"] == "session/load"
        assert session_req["params"]["sessionId"] == "prev-123"
        assert session_req["params"]["cwd"] == "."
        assert session_req["params"]["mcpServers"] == []

    @pytest.mark.asyncio
    async def test_start_with_resume_preserves_requested_session_id_on_null_result(self) -> None:
        proc = _mock_process(stdout_lines=_resume_handshake_lines_without_session_id())
        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start(session_id="prev-123")

        assert client.session_id == "prev-123"

    @pytest.mark.asyncio
    async def test_create_session_uses_init_notification_session_id_when_response_is_empty(
        self,
    ) -> None:
        proc = _mock_process(
            stdout_lines=_initialize_only_lines()
            + [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/init",
                        "params": {"session_id": "notif-123", "model": "auto-gemini-3"},
                    }
                )
                + "\n",
                json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}}) + "\n",
            ]
        )

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start(auto_session=False)
                result = await client.create_session(model="auto-gemini-3")

        assert result["sessionId"] == "notif-123"
        assert client.session_id == "notif-123"

    @pytest.mark.asyncio
    async def test_start_raises_when_cli_not_found(self) -> None:
        with patch("gobby.adapters.acp_client.shutil.which", return_value=None):
            client = GeminiACPClient()
            with pytest.raises(FileNotFoundError, match="Gemini CLI not found"):
                await client.start()

    @pytest.mark.asyncio
    async def test_start_raises_when_already_started(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
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
        proc = _mock_process(stdout_lines=_handshake_lines())
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ) as mock_exec:
            client = GeminiACPClient(cli_path="/custom/gemini")
            await client.start()

            call_args = mock_exec.call_args
            assert call_args[0][0] == "/custom/gemini"

    @pytest.mark.asyncio
    async def test_start_includes_stderr_when_initialize_hits_eof(self) -> None:
        proc = _mock_process(returncode=1, stderr_text="auth failed\ntry again\n")
        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                with pytest.raises(
                    RuntimeError,
                    match="EOF while waiting for initialize response; stderr: auth failed try again",
                ):
                    await client.start(auto_session=False)


# ---------------------------------------------------------------------------
# Send -- session/prompt + notification parsing
# ---------------------------------------------------------------------------


class TestSend:
    @pytest.mark.asyncio
    async def test_send_writes_session_prompt_request(self) -> None:
        # Handshake lines + prompt response
        prompt_response = json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"stats": {}}}) + "\n"
        proc = _mock_process(stdout_lines=_handshake_lines() + [prompt_response])

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                events = [e async for e in client.send("hello")]

        # Third write = the prompt request (after initialize + session/new)
        assert proc.stdin.write.call_count == 3
        prompt_req = json.loads(proc.stdin.write.call_args_list[2][0][0].decode())
        assert prompt_req["method"] == "session/prompt"
        assert prompt_req["params"]["sessionId"] == "sess-1"
        assert prompt_req["params"]["prompt"] == [{"type": "text", "text": "hello"}]

        # Should have a result event from the response
        assert len(events) == 1
        assert events[0].event_type == "result"

    @pytest.mark.asyncio
    async def test_send_yields_content_delta_from_notifications(self) -> None:
        # Notifications followed by final response
        notification_lines = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/message",
                    "params": {"role": "assistant", "delta": True, "content": "Hello "},
                }
            )
            + "\n",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/message",
                    "params": {"role": "assistant", "delta": True, "content": "world!"},
                }
            )
            + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"stats": {"tokens": 10}}}) + "\n",
        ]
        proc = _mock_process(stdout_lines=_handshake_lines() + notification_lines)

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
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
    async def test_send_yields_content_delta_from_session_update_notifications(self) -> None:
        notification_lines = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "sess-1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "Hello from update"},
                        },
                    },
                }
            )
            + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"stats": {"tokens": 10}}}) + "\n",
        ]
        proc = _mock_process(stdout_lines=_handshake_lines() + notification_lines)

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                events = [e async for e in client.send("say hello")]

        deltas = [e for e in events if e.event_type == "content_delta"]
        assert len(deltas) == 1
        assert deltas[0].data["content"] == "Hello from update"

    @pytest.mark.asyncio
    async def test_send_yields_error_from_response(self) -> None:
        error_response = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "error": {"code": 429, "message": "rate limited"},
                }
            )
            + "\n"
        )
        proc = _mock_process(stdout_lines=_handshake_lines() + [error_response])

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
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
        assert events[0].data["code"] == 429

    @pytest.mark.asyncio
    async def test_send_raises_when_not_started(self) -> None:
        client = GeminiACPClient()
        with pytest.raises(RuntimeError, match="not started"):
            async for _ in client.send("hi"):
                pass

    @pytest.mark.asyncio
    async def test_send_raises_when_process_exited(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines(), returncode=None)

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
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
            json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"stats": {}}}) + "\n",
        ]
        proc = _mock_process(stdout_lines=_handshake_lines() + lines)

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
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

    @pytest.mark.asyncio
    async def test_send_handles_init_notification(self) -> None:
        """Verify session/init notification during prompt is captured."""
        notification_lines = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/init",
                    "params": {"session_id": "s1", "model": "gemini-2.5-pro"},
                }
            )
            + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"stats": {}}}) + "\n",
        ]
        proc = _mock_process(stdout_lines=_handshake_lines() + notification_lines)

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                events = [e async for e in client.send("hi")]

        assert events[0].event_type == "init"
        assert events[0].data["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_send_uses_prompt_timeout_for_slow_first_response(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        handshake_iter = iter(_handshake_lines())

        async def _slow_readline() -> bytes:
            try:
                return next(handshake_iter).encode()
            except StopIteration:
                pass
            await wait_forever()
            return b""

        proc.stdout.readline = AsyncMock(side_effect=_slow_readline)

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient(prompt_timeout=0.01)
                await client.start()
                with pytest.raises(TimeoutError, match="after 0.0s"):
                    async for _ in client.send("slow"):
                        pass


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_closes_stdin_and_waits_for_eof_exit(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        wait_for_timeouts: list[float] = []

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()
                assert client.is_started

            with patch(
                "gobby.adapters.acp_client.asyncio.wait_for",
                new=_recording_wait_for(wait_for_timeouts),
            ):
                await client.stop()

        assert not client.is_started
        assert client.session_id is None
        proc.stdin.close.assert_called_once()
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
        assert wait_for_timeouts == [15.0]

    @pytest.mark.asyncio
    async def test_stop_eof_timeout_escalates_to_terminate(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        proc.wait.side_effect = [TimeoutError(), None]
        wait_for_timeouts: list[float] = []

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient()
                await client.start()

            with patch(
                "gobby.adapters.acp_client.asyncio.wait_for",
                new=_recording_wait_for(wait_for_timeouts),
            ):
                await client.stop()

        proc.stdin.close.assert_called_once()
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
        assert wait_for_timeouts == [15.0, 15.0]

    @pytest.mark.asyncio
    async def test_stop_idle_terminate_timeout_kills_and_logs_debug_context(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        proc.wait.side_effect = [TimeoutError(), TimeoutError(), None]
        wait_for_timeouts: list[float] = []

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient(purpose="model-discovery")
                await client.start()

            with (
                patch(
                    "gobby.adapters.acp_client.asyncio.wait_for",
                    new=_recording_wait_for(wait_for_timeouts),
                ),
                patch("gobby.adapters.acp_client.logger.warning") as warning,
                patch("gobby.adapters.acp_client.logger.debug") as debug,
            ):
                await client.stop()

        proc.stdin.close.assert_called_once()
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert wait_for_timeouts == [15.0, 15.0]
        warning.assert_not_called()
        forced_cleanup_calls = [
            call
            for call in debug.call_args_list
            if "did not exit after terminate; killing" in call.args[0]
        ]
        assert len(forced_cleanup_calls) == 1
        message = forced_cleanup_calls[0].args[0] % forced_cleanup_calls[0].args[1:]
        assert "provider=gemini" in message
        assert "pid=12345" in message
        assert "purpose=model-discovery" in message

    @pytest.mark.asyncio
    async def test_stop_active_terminate_timeout_kills_and_logs_warning_context(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines())
        proc.wait.side_effect = [TimeoutError(), TimeoutError(), None]
        wait_for_timeouts: list[float] = []

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
            with patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=proc,
            ):
                client = GeminiACPClient(purpose="runtime")
                await client.start()
                client._active_operations = 1

            with (
                patch(
                    "gobby.adapters.acp_client.asyncio.wait_for",
                    new=_recording_wait_for(wait_for_timeouts),
                ),
                patch("gobby.adapters.acp_client.logger.warning") as warning,
                patch("gobby.adapters.acp_client.logger.debug") as debug,
            ):
                await client.stop()

        proc.stdin.close.assert_called_once()
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert wait_for_timeouts == [15.0, 15.0]
        warning.assert_called_once()
        message = warning.call_args.args[0] % warning.call_args.args[1:]
        assert "provider=gemini" in message
        assert "pid=12345" in message
        assert "purpose=runtime" in message
        assert not any(
            "did not exit after terminate; killing" in call.args[0] for call in debug.call_args_list
        )

    @pytest.mark.asyncio
    async def test_stop_when_not_started_is_noop(self) -> None:
        client = GeminiACPClient()
        await client.stop()  # Should not raise
        assert not client.is_started

    @pytest.mark.asyncio
    async def test_stop_when_process_already_exited(self) -> None:
        proc = _mock_process(stdout_lines=_handshake_lines(), returncode=None)

        with patch("gobby.adapters.acp_client.shutil.which", return_value="/usr/bin/gemini"):
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
        proc.stdin.close.assert_not_called()
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()


# ---------------------------------------------------------------------------
# Event normalization -- notifications
# ---------------------------------------------------------------------------


class TestNormalizeNotification:
    def test_session_init_notification(self) -> None:
        event = GeminiACPClient._normalize_notification(
            {"method": "session/init", "params": {"session_id": "abc", "version": "1.0"}}
        )
        assert event.event_type == "init"
        assert event.data["session_id"] == "abc"

    def test_session_message_delta(self) -> None:
        event = GeminiACPClient._normalize_notification(
            {
                "method": "session/message",
                "params": {"role": "assistant", "delta": True, "content": "hi"},
            }
        )
        assert event.event_type == "content_delta"
        assert event.data["content"] == "hi"

    def test_session_message_non_delta(self) -> None:
        event = GeminiACPClient._normalize_notification(
            {
                "method": "session/message",
                "params": {"role": "assistant", "content": "full response"},
            }
        )
        assert event.event_type == "message"
        assert event.data["content"] == "full response"

    def test_session_error_notification(self) -> None:
        event = GeminiACPClient._normalize_notification(
            {
                "method": "session/error",
                "params": {"message": "bad request", "code": 400},
            }
        )
        assert event.event_type == "error"
        assert event.data["message"] == "bad request"
        assert event.data["code"] == 400

    def test_session_update_agent_message_chunk(self) -> None:
        event = GeminiACPClient._normalize_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "abc",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hi from update"},
                    },
                },
            }
        )
        assert event.event_type == "content_delta"
        assert event.data["content"] == "hi from update"

    def test_session_update_agent_thought_chunk(self) -> None:
        event = GeminiACPClient._normalize_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "abc",
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "thinking"},
                    },
                },
            }
        )
        assert event.event_type == "thinking_delta"
        assert event.data["content"] == "thinking"

    def test_legacy_type_based_init(self) -> None:
        """Legacy format with 'type' field instead of 'method'."""
        event = GeminiACPClient._normalize_notification({"type": "init", "session_id": "abc"})
        assert event.event_type == "init"
        assert event.data["session_id"] == "abc"

    def test_legacy_type_based_content_delta(self) -> None:
        event = GeminiACPClient._normalize_notification(
            {"type": "message", "role": "assistant", "delta": True, "content": "hi"}
        )
        assert event.event_type == "content_delta"
        assert event.data["content"] == "hi"

    def test_legacy_type_based_error(self) -> None:
        event = GeminiACPClient._normalize_notification(
            {"type": "error", "message": "bad request", "code": 400}
        )
        assert event.event_type == "error"
        assert event.data["message"] == "bad request"

    def test_unknown_notification_passes_through(self) -> None:
        event = GeminiACPClient._normalize_notification(
            {"method": "custom/event", "params": {"foo": "bar"}}
        )
        assert event.event_type == "custom/event"
        assert event.data["foo"] == "bar"
