"""Tests for hook_dispatcher.py fail-closed behavior and agent self-termination."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# The dispatcher lives outside the package tree — import via path manipulation
sys.path.insert(
    0,
    str(
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "src"
        / "gobby"
        / "install"
        / "shared"
        / "hooks"
    ),
)

import hook_dispatcher  # noqa: E402

# ── Fixtures ─────────────────────────────────────────────────────────────

CLAUDE_CONFIG = hook_dispatcher.CLI_CONFIGS["claude"]


@pytest.fixture()
def _patch_daemon_running() -> Iterator[None]:
    """Assume daemon is running for all tests."""
    with patch.object(
        hook_dispatcher, "check_daemon_running", new_callable=AsyncMock, return_value=True
    ):
        yield


@pytest.fixture()
def _patch_stdin():
    """Provide valid JSON on stdin."""
    with patch("sys.stdin", StringIO('{"prompt": "test"}')):
        yield


@pytest.fixture()
def _patch_args():
    """Patch argument parsing to return a session-start hook for claude."""
    args = MagicMock()
    args.type = "session-start"
    args.debug = False
    args.cli = "claude"
    with patch.object(hook_dispatcher, "parse_arguments", return_value=args):
        with patch.object(hook_dispatcher, "detect_cli", return_value=CLAUDE_CONFIG):
            yield args


# ── Tests ────────────────────────────────────────────────────────────────


class TestFailClosedOnCriticalHooks:
    """Critical hooks (session-start, session-end, pre-compact) must return exit code 2 on errors."""

    @pytest.mark.asyncio
    async def test_daemon_http_error_blocks_critical_hook(
        self, _patch_daemon_running, _patch_stdin, _patch_args
    ) -> None:
        """Daemon returns 500 on a critical hook → exit code 2 (block)."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch.object(
                hook_dispatcher,
                "get_daemon_url",
                new_callable=AsyncMock,
                return_value="http://localhost:60887",
            ):
                exit_code = await hook_dispatcher.main()

        assert exit_code == 2, f"Critical hook error should block (exit 2), got {exit_code}"

    @pytest.mark.asyncio
    async def test_daemon_http_error_allows_non_critical_hook(
        self, _patch_daemon_running, _patch_stdin
    ) -> None:
        """Daemon returns 500 on a non-critical hook → exit code 1 (allow)."""
        args = MagicMock()
        args.type = "post-tool-use"  # Not in critical_hooks
        args.debug = False
        args.cli = "claude"

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(hook_dispatcher, "parse_arguments", return_value=args):
            with patch.object(hook_dispatcher, "detect_cli", return_value=CLAUDE_CONFIG):
                with patch("httpx.AsyncClient", return_value=mock_client):
                    with patch.object(
                        hook_dispatcher,
                        "get_daemon_url",
                        new_callable=AsyncMock,
                        return_value="http://localhost:60887",
                    ):
                        exit_code = await hook_dispatcher.main()

        assert exit_code == 1, f"Non-critical hook error should allow (exit 1), got {exit_code}"

    @pytest.mark.asyncio
    async def test_connect_error_blocks_critical_hook(
        self, _patch_daemon_running, _patch_stdin, _patch_args
    ) -> None:
        """httpx.ConnectError on a critical hook → exit code 2."""
        import httpx

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch.object(
                hook_dispatcher,
                "get_daemon_url",
                new_callable=AsyncMock,
                return_value="http://localhost:60887",
            ):
                exit_code = await hook_dispatcher.main()

        assert exit_code == 2

    @pytest.mark.asyncio
    async def test_timeout_blocks_critical_hook(
        self, _patch_daemon_running, _patch_stdin, _patch_args
    ) -> None:
        """httpx.TimeoutException on a critical hook → exit code 2."""
        import httpx

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch.object(
                hook_dispatcher,
                "get_daemon_url",
                new_callable=AsyncMock,
                return_value="http://localhost:60887",
            ):
                exit_code = await hook_dispatcher.main()

        assert exit_code == 2

    @pytest.mark.asyncio
    async def test_generic_exception_blocks_critical_hook(
        self, _patch_daemon_running, _patch_stdin, _patch_args
    ) -> None:
        """Generic exception on a critical hook → exit code 2."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=RuntimeError("unexpected"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch.object(
                hook_dispatcher,
                "get_daemon_url",
                new_callable=AsyncMock,
                return_value="http://localhost:60887",
            ):
                exit_code = await hook_dispatcher.main()

        assert exit_code == 2

    @pytest.mark.asyncio
    async def test_successful_block_still_works(
        self, _patch_daemon_running, _patch_stdin, _patch_args
    ) -> None:
        """Normal block response from daemon → exit code 2."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "decision": "block",
            "reason": "Task still in_progress",
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch.object(
                hook_dispatcher,
                "get_daemon_url",
                new_callable=AsyncMock,
                return_value="http://localhost:60887",
            ):
                exit_code = await hook_dispatcher.main()

        assert exit_code == 2

    @pytest.mark.asyncio
    async def test_successful_allow_still_works(
        self, _patch_daemon_running, _patch_stdin, _patch_args
    ) -> None:
        """Normal allow response from daemon → exit code 0."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "decision": "approve",
            "continue": True,
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch.object(
                hook_dispatcher,
                "get_daemon_url",
                new_callable=AsyncMock,
                return_value="http://localhost:60887",
            ):
                exit_code = await hook_dispatcher.main()

        assert exit_code == 0


CODEX_CONFIG = hook_dispatcher.CLI_CONFIGS["codex"]


class TestCodexStopHookBlocking:
    """Codex Stop hook must use exit code 2 to block, not JSON body with exit 0."""

    @pytest.mark.asyncio
    async def test_codex_stop_block_returns_exit_2(
        self, _patch_daemon_running, _patch_stdin
    ) -> None:
        """Block decision on Codex Stop hook -> exit code 2 (not 0)."""
        args = MagicMock()
        args.type = "Stop"
        args.debug = False
        args.cli = "codex"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "decision": "block",
            "reason": "Task still in_progress",
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(hook_dispatcher, "parse_arguments", return_value=args):
            with patch.object(hook_dispatcher, "detect_cli", return_value=CODEX_CONFIG):
                with patch("httpx.AsyncClient", return_value=mock_client):
                    with patch.object(
                        hook_dispatcher,
                        "get_daemon_url",
                        new_callable=AsyncMock,
                        return_value="http://localhost:60887",
                    ):
                        exit_code = await hook_dispatcher.main()

        assert exit_code == 2

    @pytest.mark.asyncio
    async def test_codex_pretooluse_block_returns_exit_0(
        self, _patch_daemon_running, _patch_stdin, capsys
    ) -> None:
        """Block decision on Codex PreToolUse -> exit code 0 with JSON body."""
        args = MagicMock()
        args.type = "PreToolUse"
        args.debug = False
        args.cli = "codex"

        block_result = {
            "decision": "block",
            "reason": "Tool not allowed",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = block_result

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(hook_dispatcher, "parse_arguments", return_value=args):
            with patch.object(hook_dispatcher, "detect_cli", return_value=CODEX_CONFIG):
                with patch("httpx.AsyncClient", return_value=mock_client):
                    with patch.object(
                        hook_dispatcher,
                        "get_daemon_url",
                        new_callable=AsyncMock,
                        return_value="http://localhost:60887",
                    ):
                        exit_code = await hook_dispatcher.main()

        assert exit_code == 0
        captured = capsys.readouterr()
        import json

        output = json.loads(captured.out)
        assert output["decision"] == "block"

    @pytest.mark.asyncio
    async def test_codex_stop_allow_returns_exit_0(
        self, _patch_daemon_running, _patch_stdin
    ) -> None:
        """Allow decision on Codex Stop hook -> exit code 0 (stop proceeds)."""
        args = MagicMock()
        args.type = "Stop"
        args.debug = False
        args.cli = "codex"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "decision": "allow",
            "continue": True,
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(hook_dispatcher, "parse_arguments", return_value=args):
            with patch.object(hook_dispatcher, "detect_cli", return_value=CODEX_CONFIG):
                with patch("httpx.AsyncClient", return_value=mock_client):
                    with patch.object(
                        hook_dispatcher,
                        "get_daemon_url",
                        new_callable=AsyncMock,
                        return_value="http://localhost:60887",
                    ):
                        exit_code = await hook_dispatcher.main()

        assert exit_code == 0


class TestAgentDaemonFailureTracking:
    """Spawned agents force-kill after consecutive daemon-down detections."""

    @pytest.fixture(autouse=True)
    def _clean_failure_dir(self, tmp_path: Path):
        """Use tmp_path for failure tracking to avoid polluting /tmp."""
        with patch.object(hook_dispatcher, "_DAEMON_FAILURE_DIR", tmp_path / "failures"):
            yield

    def test_track_increments_counter(self) -> None:
        """Each call to _track_daemon_failure increments the counter."""
        assert hook_dispatcher._track_daemon_failure("test-run-1") == 1
        assert hook_dispatcher._track_daemon_failure("test-run-1") == 2
        assert hook_dispatcher._track_daemon_failure("test-run-1") == 3

    def test_track_independent_per_agent(self) -> None:
        """Different agent run IDs have independent counters."""
        assert hook_dispatcher._track_daemon_failure("agent-a") == 1
        assert hook_dispatcher._track_daemon_failure("agent-b") == 1
        assert hook_dispatcher._track_daemon_failure("agent-a") == 2
        assert hook_dispatcher._track_daemon_failure("agent-b") == 2

    def test_reset_clears_counter(self) -> None:
        """_reset_daemon_failures clears the counter."""
        hook_dispatcher._track_daemon_failure("test-run-2")
        hook_dispatcher._track_daemon_failure("test-run-2")
        hook_dispatcher._reset_daemon_failures("test-run-2")
        # Next track should start from 1 again
        assert hook_dispatcher._track_daemon_failure("test-run-2") == 1

    def test_reset_noop_if_no_file(self) -> None:
        """_reset_daemon_failures is safe to call with no existing file."""
        hook_dispatcher._reset_daemon_failures("nonexistent-run")  # Should not raise

    @pytest.mark.asyncio
    async def test_force_kill_after_max_failures(self, _patch_stdin) -> None:
        """After MAX_DAEMON_FAILURES, _force_kill_agent is called."""
        args = MagicMock()
        args.type = "BeforeAgent"
        args.debug = False
        args.cli = "gemini"

        gemini_config = hook_dispatcher.CLI_CONFIGS["gemini"]

        # Pre-seed failures to MAX - 1
        for _ in range(hook_dispatcher._MAX_DAEMON_FAILURES - 1):
            hook_dispatcher._track_daemon_failure("test-force-kill")

        with (
            patch.object(
                hook_dispatcher, "check_daemon_running", new_callable=AsyncMock, return_value=False
            ),
            patch.object(hook_dispatcher, "parse_arguments", return_value=args),
            patch.object(hook_dispatcher, "detect_cli", return_value=gemini_config),
            patch.object(hook_dispatcher, "_force_kill_agent") as mock_kill,
            patch.dict(os.environ, {"GOBBY_AGENT_RUN_ID": "test-force-kill"}),
        ):
            exit_code = await hook_dispatcher.main()

        assert exit_code == 2
        mock_kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_kill_below_threshold(self, _patch_stdin) -> None:
        """Before MAX_DAEMON_FAILURES, _force_kill_agent is NOT called."""
        args = MagicMock()
        args.type = "BeforeAgent"
        args.debug = False
        args.cli = "gemini"

        gemini_config = hook_dispatcher.CLI_CONFIGS["gemini"]

        with (
            patch.object(
                hook_dispatcher, "check_daemon_running", new_callable=AsyncMock, return_value=False
            ),
            patch.object(hook_dispatcher, "parse_arguments", return_value=args),
            patch.object(hook_dispatcher, "detect_cli", return_value=gemini_config),
            patch.object(hook_dispatcher, "_force_kill_agent") as mock_kill,
            patch.dict(os.environ, {"GOBBY_AGENT_RUN_ID": "test-no-kill"}),
        ):
            exit_code = await hook_dispatcher.main()

        assert exit_code == 2
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_on_successful_daemon_connection(self, _patch_stdin) -> None:
        """Successful daemon connection resets the failure counter."""
        args = MagicMock()
        args.type = "BeforeAgent"
        args.debug = False
        args.cli = "gemini"

        gemini_config = hook_dispatcher.CLI_CONFIGS["gemini"]

        # Pre-seed some failures
        hook_dispatcher._track_daemon_failure("test-reset")
        hook_dispatcher._track_daemon_failure("test-reset")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"decision": "allow"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(
                hook_dispatcher, "check_daemon_running", new_callable=AsyncMock, return_value=True
            ),
            patch.object(hook_dispatcher, "parse_arguments", return_value=args),
            patch.object(hook_dispatcher, "detect_cli", return_value=gemini_config),
            patch("httpx.AsyncClient", return_value=mock_client),
            patch.object(
                hook_dispatcher,
                "get_daemon_url",
                new_callable=AsyncMock,
                return_value="http://localhost:60887",
            ),
            patch.dict(os.environ, {"GOBBY_AGENT_RUN_ID": "test-reset"}),
        ):
            exit_code = await hook_dispatcher.main()

        assert exit_code == 0
        # Counter should be reset — next failure starts at 1
        assert hook_dispatcher._track_daemon_failure("test-reset") == 1
