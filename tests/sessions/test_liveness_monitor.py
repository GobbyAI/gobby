"""Tests for gobby.sessions.liveness_monitor module.

Tests for the SessionLivenessMonitor that detects dead CLI sessions
via parent PID checks and triggers session expiry + summary generation.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.liveness_monitor import SessionLivenessMonitor

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_session_storage():
    storage = MagicMock()
    storage.db = MagicMock()
    return storage


@pytest.fixture
def mock_dispatch_fn():
    return MagicMock()


@pytest.fixture
def mock_processor():
    return MagicMock()


@pytest.fixture
def monitor(mock_session_storage, mock_dispatch_fn, mock_processor):
    return SessionLivenessMonitor(
        session_storage=mock_session_storage,
        dispatch_summaries_fn=mock_dispatch_fn,
        message_processor=mock_processor,
        poll_interval=1.0,
    )


class TestSessionLivenessMonitor:
    """Core monitor logic tests."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self, monitor):
        """Test lifecycle start/stop."""
        await monitor.start()
        assert monitor._task is not None
        assert not monitor._task.done()

        await monitor.stop()
        assert monitor._task is None

    @pytest.mark.asyncio
    async def test_start_idempotent(self, monitor):
        """Starting twice doesn't create duplicate tasks."""
        await monitor.start()
        task1 = monitor._task
        await monitor.start()
        assert monitor._task is task1
        await monitor.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self, monitor):
        """Stopping without starting is safe."""
        await monitor.stop()
        assert monitor._task is None

    def test_mark_recently_handled(self, monitor):
        """Test deduplication via mark_recently_handled."""
        monitor.mark_recently_handled("session-1")
        assert "session-1" in monitor._recently_handled


class TestCheckSessions:
    """Tests for _check_sessions detection logic."""

    @pytest.mark.asyncio
    async def test_detects_dead_pid(self, monitor, mock_session_storage, mock_dispatch_fn):
        """Dead parent PID (no tmux) triggers expiry + summary dispatch."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"parent_pid": 99999})},
        ]

        with patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=False):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("s1", False, None)
        mock_session_storage.update_status.assert_called_once_with("s1", "expired")
        assert "s1" in monitor._recently_handled

    @pytest.mark.asyncio
    async def test_ignores_alive_pid(self, monitor, mock_session_storage, mock_dispatch_fn):
        """Alive parent PID means session is still active — no action."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"parent_pid": 99999})},
        ]

        with patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=True):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()
        mock_session_storage.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_recently_handled(self, monitor, mock_session_storage, mock_dispatch_fn):
        """Sessions in the recently-handled set are skipped."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"parent_pid": 99999})},
        ]

        monitor.mark_recently_handled("s1")

        with patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=False):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_missing_parent_pid(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ) -> None:
        """Sessions without parent_pid in terminal_context are skipped."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"tmux_pane": "%1"})},
        ]

        await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_invalid_json(self, monitor, mock_session_storage, mock_dispatch_fn):
        """Invalid JSON in terminal_context is skipped gracefully."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": "not-json"},
        ]

        await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_empty_results(self, monitor, mock_session_storage, mock_dispatch_fn):
        """No active sessions means no work."""
        mock_session_storage.db.fetchall.return_value = []

        await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_sessions_mixed(self, monitor, mock_session_storage, mock_dispatch_fn):
        """Multiple sessions: only dead PIDs trigger expiry."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "alive", "terminal_context": json.dumps({"parent_pid": 100})},
            {"id": "dead", "terminal_context": json.dumps({"parent_pid": 200})},
        ]

        def pid_check(pid):
            return pid == 100  # 100 is alive, 200 is dead

        with patch.object(SessionLivenessMonitor, "_is_pid_alive", side_effect=pid_check):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("dead", False, None)
        mock_session_storage.update_status.assert_called_once_with("dead", "expired")

    @pytest.mark.asyncio
    async def test_recently_handled_ttl_expiry(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ) -> None:
        """Entries in recently-handled set expire after TTL."""
        import time

        # Add entry with expired timestamp
        monitor._recently_handled["s1"] = time.monotonic() - 200  # well past TTL

        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"parent_pid": 99999})},
        ]

        with patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=False):
            await monitor._check_sessions()

        # TTL expired, so s1 should be processed again
        mock_dispatch_fn.assert_called_once_with("s1", False, None)

    @pytest.mark.asyncio
    async def test_dead_pid_live_tmux_pane_refreshes(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """Dead parent PID + live tmux pane → touch session, do NOT expire."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "terminal_context": json.dumps({"parent_pid": 99999, "tmux_pane": "%6"}),
            },
        ]

        with (
            patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=False),
            patch.object(SessionLivenessMonitor, "_is_tmux_pane_alive", return_value=True),
        ):
            await monitor._check_sessions()

        # Session should be touched, NOT expired
        mock_session_storage.touch.assert_called_once_with("s1")
        mock_dispatch_fn.assert_not_called()
        mock_session_storage.update_status.assert_not_called()
        assert "s1" not in monitor._recently_handled

    @pytest.mark.asyncio
    async def test_dead_pid_dead_tmux_pane_expires(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """Dead parent PID + dead tmux pane → expire as normal."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "terminal_context": json.dumps({"parent_pid": 99999, "tmux_pane": "%6"}),
            },
        ]

        with (
            patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=False),
            patch.object(SessionLivenessMonitor, "_is_tmux_pane_alive", return_value=False),
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("s1", False, None)
        mock_session_storage.update_status.assert_called_once_with("s1", "expired")
        assert "s1" in monitor._recently_handled

    @pytest.mark.asyncio
    async def test_dead_pid_no_tmux_pane_expires(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """Dead parent PID + no tmux pane → expire as normal."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "terminal_context": json.dumps({"parent_pid": 99999}),
            },
        ]

        with patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=False):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("s1", False, None)
        mock_session_storage.update_status.assert_called_once_with("s1", "expired")

    @pytest.mark.asyncio
    async def test_dead_pid_live_tmux_touch_failure_continues(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """If touch() fails for a live tmux session, don't expire — just continue."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "terminal_context": json.dumps({"parent_pid": 99999, "tmux_pane": "%6"}),
            },
        ]
        mock_session_storage.touch.side_effect = Exception("DB error")

        with (
            patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=False),
            patch.object(SessionLivenessMonitor, "_is_tmux_pane_alive", return_value=True),
        ):
            await monitor._check_sessions()

        # Should NOT expire even if touch fails
        mock_dispatch_fn.assert_not_called()
        mock_session_storage.update_status.assert_not_called()


class TestExpireSession:
    """Tests for _expire_session."""

    @pytest.mark.asyncio
    async def test_dispatches_summaries_and_expires(
        self, monitor, mock_session_storage, mock_dispatch_fn, mock_processor
    ):
        """Full expire flow: dispatch summaries, update status, unregister."""
        await monitor._expire_session("s1")

        mock_dispatch_fn.assert_called_once_with("s1", False, None)
        mock_session_storage.update_status.assert_called_once_with("s1", "expired")
        mock_processor.unregister_session.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_summary_dispatch_failure_continues(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """If summary dispatch fails, session is still expired."""
        mock_dispatch_fn.side_effect = Exception("LLM down")

        await monitor._expire_session("s1")

        mock_session_storage.update_status.assert_called_once_with("s1", "expired")

    @pytest.mark.asyncio
    async def test_status_update_failure_logged(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """If status update fails, no crash."""
        mock_session_storage.update_status.side_effect = Exception("DB error")

        # Should not raise
        await monitor._expire_session("s1")

    @pytest.mark.asyncio
    async def test_falls_back_to_generate_fn(self, mock_session_storage, mock_processor):
        """Uses generate_summaries_fn when dispatch_summaries_fn is not available."""
        gen_fn = AsyncMock()
        mon = SessionLivenessMonitor(
            session_storage=mock_session_storage,
            dispatch_summaries_fn=None,
            generate_summaries_fn=gen_fn,
            message_processor=mock_processor,
        )

        await mon._expire_session("s1")

        gen_fn.assert_awaited_once_with("s1")
        mock_session_storage.update_status.assert_called_once_with("s1", "expired")

    @pytest.mark.asyncio
    async def test_no_summary_fn_still_expires(self, mock_session_storage, mock_processor):
        """If neither summary function is available, session is still expired."""
        mon = SessionLivenessMonitor(
            session_storage=mock_session_storage,
            dispatch_summaries_fn=None,
            generate_summaries_fn=None,
            message_processor=mock_processor,
        )

        await mon._expire_session("s1")

        mock_session_storage.update_status.assert_called_once_with("s1", "expired")


class TestIsPidAlive:
    """Tests for the static _is_pid_alive method."""

    def test_alive_pid(self):
        """Current process PID should be alive."""
        import os

        assert SessionLivenessMonitor._is_pid_alive(os.getpid()) is True

    def test_dead_pid(self):
        """Non-existent PID should be dead."""
        # Use a very high PID that's unlikely to exist
        with patch("os.kill", side_effect=ProcessLookupError):
            assert SessionLivenessMonitor._is_pid_alive(999999999) is False

    def test_permission_error_means_alive(self):
        """PermissionError means the process exists but we can't signal it."""
        with patch("os.kill", side_effect=PermissionError):
            assert SessionLivenessMonitor._is_pid_alive(1) is True

    def test_os_error_means_dead(self):
        """Generic OSError means dead."""
        with patch("os.kill", side_effect=OSError):
            assert SessionLivenessMonitor._is_pid_alive(1) is False


class TestIsTmuxPaneAlive:
    """Tests for the static _is_tmux_pane_alive method."""

    def test_alive_pane(self):
        """Pane ID in tmux output means pane is alive."""
        mock_result = MagicMock()
        mock_result.stdout = "%5\n%6\n%7\n"
        with patch("subprocess.run", return_value=mock_result):
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6") is True

    def test_dead_pane(self):
        """Pane ID not in tmux output means pane is dead."""
        mock_result = MagicMock()
        mock_result.stdout = "%5\n%7\n"
        with patch("subprocess.run", return_value=mock_result):
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6") is False

    def test_alive_pane_with_socket_path(self):
        """When a socket path is known, liveness checks that exact tmux server."""
        mock_result = MagicMock()
        mock_result.stdout = "%6\n"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6", "/tmp/tmux-1000/gobby") is True

        mock_run.assert_called_once_with(
            ["tmux", "-S", "/tmp/tmux-1000/gobby", "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_dead_pane_checks_default_and_gobby_socket_when_path_unknown(self):
        """Legacy rows without a socket path check both the default server and Gobby's socket."""
        default_result = MagicMock()
        default_result.stdout = ""
        gobby_result = MagicMock()
        gobby_result.stdout = "%6\n"

        with patch("subprocess.run", side_effect=[default_result, gobby_result]) as mock_run:
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6") is True

        assert mock_run.call_args_list[0].args[0] == [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}",
        ]
        assert mock_run.call_args_list[1].args[0] == [
            "tmux",
            "-L",
            "gobby",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}",
        ]

    def test_tmux_not_installed(self):
        """FileNotFoundError (tmux not installed) returns False."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6") is False

    def test_tmux_timeout(self):
        """Subprocess timeout returns False."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tmux", 5)):
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6") is False

    def test_os_error(self):
        """Generic OSError returns False."""
        with patch("subprocess.run", side_effect=OSError("tmux server not running")):
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6") is False

    def test_empty_output(self):
        """Empty tmux output (no panes) returns False."""
        mock_result = MagicMock()
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6") is False


class TestGetActiveSessionsWithPid:
    """Tests for _get_active_sessions_with_pid query."""

    def test_parses_terminal_context(self, monitor, mock_session_storage):
        """Correctly extracts parent_pid and tmux_pane from terminal_context JSON."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"parent_pid": 12345})},
            {
                "id": "s2",
                "terminal_context": json.dumps(
                    {
                        "parent_pid": 67890,
                        "tmux_pane": "%3",
                        "tmux_socket_path": "/tmp/tmux-1000/gobby",
                    }
                ),
            },
        ]

        result = monitor._get_active_sessions_with_pid()

        assert result == [
            ("s1", 12345, None, None),
            ("s2", 67890, "%3", "/tmp/tmux-1000/gobby"),
        ]

    def test_skips_missing_pid(self, monitor, mock_session_storage):
        """Sessions without parent_pid are excluded."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"tmux_pane": "%1"})},
        ]

        result = monitor._get_active_sessions_with_pid()

        assert result == []

    def test_skips_invalid_pid(self, monitor, mock_session_storage):
        """Non-integer or zero/negative PIDs are excluded."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"parent_pid": "not-a-pid"})},
            {"id": "s2", "terminal_context": json.dumps({"parent_pid": 0})},
            {"id": "s3", "terminal_context": json.dumps({"parent_pid": -1})},
        ]

        result = monitor._get_active_sessions_with_pid()

        assert result == []

    def test_handles_db_error(self, monitor, mock_session_storage):
        """DB errors return empty list."""
        mock_session_storage.db.fetchall.side_effect = Exception("DB error")

        result = monitor._get_active_sessions_with_pid()

        assert result == []

    def test_excludes_agent_sessions(self, monitor, mock_session_storage):
        """Query filters out sessions with agent_run_id (handled by TmuxPaneMonitor)."""
        # Verify the SQL query excludes agent sessions
        mock_session_storage.db.fetchall.return_value = []

        monitor._get_active_sessions_with_pid()

        call_args = mock_session_storage.db.fetchall.call_args
        sql = call_args[0][0]
        assert "agent_run_id IS NULL" in sql

    def test_non_string_tmux_pane_treated_as_none(self, monitor, mock_session_storage):
        """Non-string tmux_pane values are normalized to None."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"parent_pid": 123, "tmux_pane": 42})},
        ]

        result = monitor._get_active_sessions_with_pid()

        assert result == [("s1", 123, None, None)]

    def test_non_string_tmux_socket_path_treated_as_none(self, monitor, mock_session_storage):
        """Non-string tmux_socket_path values are normalized to None."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "terminal_context": json.dumps(
                    {
                        "parent_pid": 123,
                        "tmux_pane": "%4",
                        "tmux_socket_path": 42,
                    }
                ),
            },
        ]

        result = monitor._get_active_sessions_with_pid()

        assert result == [("s1", 123, "%4", None)]
