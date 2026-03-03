"""Tests for AutonomousRunner — SDK-based autonomous agent execution.

Covers:
- Successful run with mocked ClaudeSDKClient
- Error handling (CLI not found, SDK error, cancellation)
- SDK session ID capture from ResultMessage
- Hook wiring (lifecycle callbacks → SDK hooks)
- Auto-approve behavior (can_use_tool always True)
- Agent run manager integration (complete/fail calls)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import real SDK types so isinstance checks work in the runner
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from claude_agent_sdk.types import StreamEvent

from gobby.agents.spawners.autonomous import AutonomousRunner

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Factory helpers using real SDK types
# ---------------------------------------------------------------------------


def _text_message(text: str) -> AssistantMessage:
    """Create an AssistantMessage with a single TextBlock."""
    return AssistantMessage(content=[TextBlock(text)], model="test-model")


def _result_message(
    result: str | None = None,
    session_id: str = "test-sdk-session",
) -> ResultMessage:
    """Create a ResultMessage with minimal required fields."""
    return ResultMessage(
        subtype="result",
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        result=result,
    )


def _stream_event() -> StreamEvent:
    """Create a StreamEvent (skipped during processing)."""
    return StreamEvent(
        uuid="test-uuid",
        session_id="test-session",
        event={"type": "message_start"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runner(**overrides: Any) -> AutonomousRunner:
    """Create an AutonomousRunner with sensible test defaults."""
    defaults = {
        "session_id": "test-session-123",
        "run_id": "test-run-456",
        "project_id": "test-project",
        "cwd": "/tmp/test-workspace",
        "prompt": "Fix the bug in main.py",
        "model": "sonnet",
        "system_prompt": "You are a test agent.",
        "max_turns": 5,
        "seq_num": 42,
    }
    defaults.update(overrides)
    return AutonomousRunner(**defaults)


def _mock_sdk_response(messages: list[Any]) -> AsyncMock:
    """Create a mock ClaudeSDKClient that yields given messages."""
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def _receive():
        for msg in messages:
            yield msg

    client.receive_response = _receive
    return client


# ---------------------------------------------------------------------------
# Successful run
# ---------------------------------------------------------------------------


class TestSuccessfulRun:
    @pytest.mark.asyncio
    async def test_basic_run_returns_text(self) -> None:
        """Runner accumulates text from AssistantMessage and ResultMessage."""
        messages = [
            _text_message("Working on it..."),
            _result_message(result="\nDone!", session_id="sdk-sess-789"),
        ]
        client = _mock_sdk_response(messages)
        runner = _make_runner()

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", return_value=client),
        ):
            result = await runner.run()

        assert "Working on it..." in result
        assert "Done!" in result
        client.connect.assert_awaited_once()
        client.query.assert_awaited_once_with("Fix the bug in main.py")
        client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stream_events_are_skipped(self) -> None:
        """StreamEvent objects are silently skipped."""
        messages = [
            _stream_event(),
            _text_message("Hello"),
            _stream_event(),
            _result_message(result=None, session_id="sdk-1"),
        ]
        client = _mock_sdk_response(messages)
        runner = _make_runner()

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", return_value=client),
        ):
            result = await runner.run()

        assert result == "Hello"


# ---------------------------------------------------------------------------
# SDK session ID capture
# ---------------------------------------------------------------------------


class TestSessionIdCapture:
    @pytest.mark.asyncio
    async def test_captures_session_id_from_result_message(self) -> None:
        """sdk_session_id is set from the first ResultMessage."""
        messages = [
            _result_message(result="ok", session_id="captured-id-abc"),
        ]
        client = _mock_sdk_response(messages)
        runner = _make_runner()

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", return_value=client),
        ):
            await runner.run()

        assert runner.sdk_session_id == "captured-id-abc"

    @pytest.mark.asyncio
    async def test_first_session_id_wins(self) -> None:
        """Only the first ResultMessage's session_id is captured."""
        messages = [
            _result_message(result="a", session_id="first-id"),
            _result_message(result="b", session_id="second-id"),
        ]
        client = _mock_sdk_response(messages)
        runner = _make_runner()

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", return_value=client),
        ):
            await runner.run()

        assert runner.sdk_session_id == "first-id"


# ---------------------------------------------------------------------------
# Agent run manager integration
# ---------------------------------------------------------------------------


class TestAgentRunManager:
    @pytest.mark.asyncio
    async def test_complete_called_on_success(self) -> None:
        """agent_run_manager.complete() is called with result text."""
        mgr = MagicMock()
        messages = [_result_message(result="all done", session_id="s1")]
        client = _mock_sdk_response(messages)
        runner = _make_runner(agent_run_manager=mgr)

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", return_value=client),
        ):
            await runner.run()

        mgr.update_sdk_session_id.assert_called_once_with("test-run-456", "s1")
        mgr.complete.assert_called_once_with("test-run-456", result="all done")

    @pytest.mark.asyncio
    async def test_fail_called_on_error(self) -> None:
        """agent_run_manager.fail() is called when SDK raises."""
        mgr = MagicMock()
        client = AsyncMock()
        client.connect = AsyncMock(side_effect=RuntimeError("SDK exploded"))
        client.disconnect = AsyncMock()

        runner = _make_runner(agent_run_manager=mgr)

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", return_value=client),
        ):
            with pytest.raises(RuntimeError, match="SDK exploded"):
                await runner.run()

        mgr.fail.assert_called_once_with("test-run-456", error="SDK exploded")

    @pytest.mark.asyncio
    async def test_fail_called_on_cancellation(self) -> None:
        """agent_run_manager.fail() is called with 'Cancelled' on CancelledError."""
        mgr = MagicMock()
        client = AsyncMock()
        client.connect = AsyncMock(side_effect=asyncio.CancelledError())
        client.disconnect = AsyncMock()

        runner = _make_runner(agent_run_manager=mgr)

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", return_value=client),
        ):
            with pytest.raises(asyncio.CancelledError):
                await runner.run()

        mgr.fail.assert_called_once_with("test-run-456", error="Cancelled")

    @pytest.mark.asyncio
    async def test_no_crash_without_agent_run_manager(self) -> None:
        """Runner works fine without an agent_run_manager."""
        messages = [_result_message(result="ok", session_id="s1")]
        client = _mock_sdk_response(messages)
        runner = _make_runner(agent_run_manager=None)

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", return_value=client),
        ):
            result = await runner.run()

        assert result == "ok"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_cli_not_found_raises(self) -> None:
        """RuntimeError when Claude CLI is not in PATH."""
        mgr = MagicMock()
        runner = _make_runner(agent_run_manager=mgr)

        with patch("gobby.agents.spawners.autonomous._find_cli_path", return_value=None):
            with pytest.raises(RuntimeError, match="Claude CLI not found"):
                await runner.run()

        mgr.fail.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_error_ignored(self) -> None:
        """Disconnect errors are swallowed — the run still completes."""
        messages = [_result_message(result="ok", session_id="s1")]
        client = _mock_sdk_response(messages)
        client.disconnect = AsyncMock(side_effect=RuntimeError("disconnect boom"))

        runner = _make_runner()

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", return_value=client),
        ):
            result = await runner.run()

        assert result == "ok"


# ---------------------------------------------------------------------------
# Auto-approve behavior
# ---------------------------------------------------------------------------


class TestAutoApprove:
    @pytest.mark.asyncio
    async def test_can_use_tool_always_true(self) -> None:
        """Autonomous agents approve all tools via can_use_tool callback."""
        captured_options = {}

        def _capture_client(options: Any) -> AsyncMock:
            captured_options["options"] = options
            return _mock_sdk_response([_result_message(result="ok", session_id="s1")])

        runner = _make_runner()

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch(
                "gobby.agents.spawners.autonomous.ClaudeSDKClient",
                side_effect=_capture_client,
            ),
        ):
            await runner.run()

        options = captured_options["options"]
        # can_use_tool is async and returns PermissionResultAllow
        from unittest.mock import MagicMock as _MagicMock

        from claude_agent_sdk.types import PermissionResultAllow

        result1 = await options.can_use_tool("Edit", {"file": "test.py"}, _MagicMock())
        result2 = await options.can_use_tool("Bash", {"command": "rm -rf /"}, _MagicMock())
        assert isinstance(result1, PermissionResultAllow)
        assert isinstance(result2, PermissionResultAllow)


# ---------------------------------------------------------------------------
# SDK options construction
# ---------------------------------------------------------------------------


class TestOptionsConstruction:
    @pytest.mark.asyncio
    async def test_system_prompt_includes_env_context(self) -> None:
        """System prompt gets environment section injected."""
        captured_options = {}

        def _capture(options: Any) -> AsyncMock:
            captured_options["options"] = options
            return _mock_sdk_response([_result_message(result="ok", session_id="s1")])

        runner = _make_runner(
            system_prompt="Base prompt.",
            cwd="/workspace/myproject",
            seq_num=42,
            project_id="proj-xyz",
        )

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", side_effect=_capture),
        ):
            await runner.run()

        sp = captured_options["options"].system_prompt
        assert "Base prompt." in sp
        assert "/workspace/myproject" in sp
        assert "#42" in sp
        assert "proj-xyz" in sp

    @pytest.mark.asyncio
    async def test_env_vars_set(self) -> None:
        """GOBBY_SESSION_ID, GOBBY_SOURCE, GOBBY_PROJECT_ID are set."""
        captured_options = {}

        def _capture(options: Any) -> AsyncMock:
            captured_options["options"] = options
            return _mock_sdk_response([_result_message(result="ok", session_id="s1")])

        runner = _make_runner(session_id="sess-abc", project_id="proj-xyz")

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", side_effect=_capture),
        ):
            await runner.run()

        env = captured_options["options"].env
        assert env["GOBBY_SESSION_ID"] == "sess-abc"
        assert env["GOBBY_SOURCE"] == "autonomous_sdk"
        assert env["GOBBY_PROJECT_ID"] == "proj-xyz"

    @pytest.mark.asyncio
    async def test_max_turns_passed_through(self) -> None:
        """max_turns from agent definition flows to ClaudeAgentOptions."""
        captured_options = {}

        def _capture(options: Any) -> AsyncMock:
            captured_options["options"] = options
            return _mock_sdk_response([_result_message(result="ok", session_id="s1")])

        runner = _make_runner(max_turns=25)

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", side_effect=_capture),
        ):
            await runner.run()

        assert captured_options["options"].max_turns == 25

    @pytest.mark.asyncio
    async def test_mcp_config_passed_when_found(self) -> None:
        """MCP config file path is passed to ClaudeAgentOptions."""
        captured_options = {}

        def _capture(options: Any) -> AsyncMock:
            captured_options["options"] = options
            return _mock_sdk_response([_result_message(result="ok", session_id="s1")])

        runner = _make_runner()

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch(
                "gobby.agents.spawners.autonomous._find_mcp_config",
                return_value="/workspace/.mcp.json",
            ),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", side_effect=_capture),
        ):
            await runner.run()

        assert captured_options["options"].mcp_servers == "/workspace/.mcp.json"


# ---------------------------------------------------------------------------
# Hook wiring
# ---------------------------------------------------------------------------


class TestHookWiring:
    @pytest.mark.asyncio
    async def test_hooks_built_when_callbacks_provided(self) -> None:
        """SDK hooks are constructed when lifecycle callbacks are set."""
        captured_options = {}

        def _capture(options: Any) -> AsyncMock:
            captured_options["options"] = options
            return _mock_sdk_response([_result_message(result="ok", session_id="s1")])

        runner = _make_runner(
            on_before_agent=AsyncMock(return_value=None),
            on_pre_tool=AsyncMock(return_value=None),
            on_post_tool=AsyncMock(return_value=None),
            on_stop=AsyncMock(return_value=None),
        )

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", side_effect=_capture),
        ):
            await runner.run()

        hooks = captured_options["options"].hooks
        assert hooks is not None
        assert "UserPromptSubmit" in hooks
        assert "PreToolUse" in hooks
        assert "PostToolUse" in hooks
        assert "Stop" in hooks

    @pytest.mark.asyncio
    async def test_no_hooks_when_no_callbacks(self) -> None:
        """No hooks dict when no lifecycle callbacks are provided."""
        captured_options = {}

        def _capture(options: Any) -> AsyncMock:
            captured_options["options"] = options
            return _mock_sdk_response([_result_message(result="ok", session_id="s1")])

        runner = _make_runner()

        with (
            patch(
                "gobby.agents.spawners.autonomous._find_cli_path", return_value="/usr/bin/claude"
            ),
            patch("gobby.agents.spawners.autonomous._find_mcp_config", return_value=None),
            patch("gobby.agents.spawners.autonomous.ClaudeSDKClient", side_effect=_capture),
        ):
            await runner.run()

        assert captured_options["options"].hooks is None

    def test_build_sdk_hooks_returns_none_without_callbacks(self) -> None:
        """_build_sdk_hooks returns None when no callbacks are set."""
        runner = _make_runner()
        assert runner._build_sdk_hooks() is None

    def test_build_sdk_hooks_partial_callbacks(self) -> None:
        """Only hooks for provided callbacks are created."""
        runner = _make_runner(
            on_before_agent=AsyncMock(return_value=None),
            on_stop=AsyncMock(return_value=None),
        )
        hooks = runner._build_sdk_hooks()
        assert hooks is not None
        assert "UserPromptSubmit" in hooks
        assert "Stop" in hooks
        assert "PreToolUse" not in hooks
        assert "PostToolUse" not in hooks
