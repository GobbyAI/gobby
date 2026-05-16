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


def _as_tuples(records):
    return [
        (record.session_id, record.parent_pid, record.tmux_pane, record.tmux_socket_path)
        for record in records
    ]


class _RecordingStorage:
    def __init__(self, *, update_error: Exception | None = None) -> None:
        self.db = MagicMock()
        self.status_updates: list[tuple[str, str]] = []
        self.update_error = update_error

    def update_status(self, session_id: str, status: str) -> None:
        if self.update_error is not None:
            raise self.update_error
        self.status_updates.append((session_id, status))


class _RecordingProcessor:
    def __init__(self) -> None:
        self.unregistered: list[str] = []

    def unregister_session(self, session_id: str) -> None:
        self.unregistered.append(session_id)


class _SummaryDispatch:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, bool, object | None]] = []
        self.error = error

    def __call__(self, session_id: str, background: bool, done_event: object | None) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((session_id, background, done_event))


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
        assert monitor._recently_handled == {}

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
        assert set(monitor._recently_handled) == {"s1"}

    @pytest.mark.asyncio
    async def test_handles_missing_parent_pid(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ) -> None:
        """Sessions without any terminal liveness metadata are skipped."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({})},
        ]

        await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()
        assert monitor._recently_handled == {}

    @pytest.mark.asyncio
    async def test_handles_invalid_json(self, monitor, mock_session_storage, mock_dispatch_fn):
        """Invalid JSON in terminal_context is skipped gracefully."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": "not-json"},
        ]

        await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()
        assert monitor._recently_handled == {}

    @pytest.mark.asyncio
    async def test_handles_empty_results(self, monitor, mock_session_storage, mock_dispatch_fn):
        """No active sessions means no work."""
        mock_session_storage.db.fetchall.return_value = []

        await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()
        assert monitor._recently_handled == {}

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
        assert set(monitor._recently_handled) == {"dead"}

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
        assert set(monitor._recently_handled) == {"s1"}

    @pytest.mark.asyncio
    async def test_dead_pid_live_tmux_pane_retained(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """Live tmux pane keeps a session active even when stored parent PID died."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "terminal_context": json.dumps({"parent_pid": 99999, "tmux_pane": "%6"}),
            },
        ]

        with (
            patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=False),
            patch.object(
                SessionLivenessMonitor,
                "_get_live_tmux_panes_by_socket",
                return_value={None: {"%6"}},
            ),
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()
        mock_session_storage.update_status.assert_not_called()
        mock_session_storage.touch.assert_called_once_with("s1")
        assert "s1" not in monitor._recently_handled

    @pytest.mark.asyncio
    async def test_live_pid_live_tmux_pane_retained(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """Live parent PID + live tmux pane leaves the session active."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "terminal_context": json.dumps({"parent_pid": 123, "tmux_pane": "%6"}),
            },
        ]

        with (
            patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=True),
            patch.object(
                SessionLivenessMonitor,
                "_get_live_tmux_panes_by_socket",
                return_value={None: {"%6"}},
            ),
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()
        mock_session_storage.update_status.assert_not_called()
        mock_session_storage.touch.assert_not_called()
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
            patch.object(
                SessionLivenessMonitor,
                "_get_live_tmux_panes_by_socket",
                return_value={None: set()},
            ),
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("s1", False, None)
        mock_session_storage.update_status.assert_called_once_with("s1", "expired")
        assert "s1" in monitor._recently_handled

    @pytest.mark.asyncio
    async def test_missing_tmux_pane_expires_even_when_parent_pid_alive(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """Destroyed tmux pane is decisive even if the recorded parent PID is alive."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "terminal_context": json.dumps({"parent_pid": 123, "tmux_pane": "%6"}),
            },
        ]

        with (
            patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=True),
            patch.object(
                SessionLivenessMonitor,
                "_get_live_tmux_panes_by_socket",
                return_value={None: set()},
            ),
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("s1", False, None)
        mock_session_storage.update_status.assert_called_once_with("s1", "expired")
        assert set(monitor._recently_handled) == {"s1"}

    @pytest.mark.asyncio
    async def test_tmux_command_failure_falls_back_to_live_pid(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """Unexpected tmux command failure should not mass-expire live sessions."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "terminal_context": json.dumps({"parent_pid": 123, "tmux_pane": "%6"}),
            },
        ]

        with (
            patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=True),
            patch.object(
                SessionLivenessMonitor,
                "_get_live_tmux_panes_by_socket",
                return_value={None: None},
            ),
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()
        mock_session_storage.update_status.assert_not_called()
        assert monitor._recently_handled == {}

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
        assert set(monitor._recently_handled) == {"s1"}

    @pytest.mark.asyncio
    async def test_legacy_pane_only_live_tmux_pane_retained(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """Legacy rows with no parent PID can stay live by tmux pane alone."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "terminal_context": json.dumps({"tmux_pane": "%6"}),
            },
        ]

        with patch.object(
            SessionLivenessMonitor,
            "_get_live_tmux_panes_by_socket",
            return_value={None: {"%6"}},
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()
        mock_session_storage.update_status.assert_not_called()
        mock_session_storage.touch.assert_called_once_with("s1")
        assert monitor._recently_handled == {}

    @pytest.mark.asyncio
    async def test_legacy_pane_only_missing_tmux_pane_expires(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """Legacy rows with no parent PID expire when their recorded pane is gone."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "terminal_context": json.dumps({"tmux_pane": "%6"}),
            },
        ]

        with patch.object(
            SessionLivenessMonitor,
            "_get_live_tmux_panes_by_socket",
            return_value={None: set()},
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("s1", False, None)
        mock_session_storage.update_status.assert_called_once_with("s1", "expired")
        mock_session_storage.touch.assert_not_called()
        assert set(monitor._recently_handled) == {"s1"}


class TestExpireSession:
    """Tests for _expire_session."""

    @pytest.mark.asyncio
    async def test_dispatches_summaries_and_expires(self):
        """Full expire flow: dispatch summaries, update status, unregister."""
        storage = _RecordingStorage()
        dispatch = _SummaryDispatch()
        processor = _RecordingProcessor()
        monitor = SessionLivenessMonitor(
            session_storage=storage,
            dispatch_summaries_fn=dispatch,
            message_processor=processor,
        )

        await monitor._expire_session("s1")

        assert dispatch.calls == [("s1", False, None)]
        assert storage.status_updates == [("s1", "expired")]
        assert processor.unregistered == ["s1"]

    @pytest.mark.asyncio
    async def test_summary_dispatch_failure_continues(self):
        """If summary dispatch fails, session is still expired."""
        storage = _RecordingStorage()
        dispatch = _SummaryDispatch(error=Exception("LLM down"))
        monitor = SessionLivenessMonitor(
            session_storage=storage,
            dispatch_summaries_fn=dispatch,
        )

        await monitor._expire_session("s1")

        assert dispatch.calls == []
        assert storage.status_updates == [("s1", "expired")]

    @pytest.mark.asyncio
    async def test_status_update_failure_logged(self):
        """If status update fails, no crash."""
        storage = _RecordingStorage(update_error=Exception("DB error"))
        dispatch = _SummaryDispatch()
        monitor = SessionLivenessMonitor(
            session_storage=storage,
            dispatch_summaries_fn=dispatch,
        )

        # Should not raise
        await monitor._expire_session("s1")
        assert dispatch.calls == [("s1", False, None)]
        assert storage.status_updates == []

    @pytest.mark.asyncio
    async def test_falls_back_to_generate_fn(self, mock_processor):
        """Uses generate_summaries_fn when dispatch_summaries_fn is not available."""
        storage = _RecordingStorage()
        gen_fn = AsyncMock()
        mon = SessionLivenessMonitor(
            session_storage=storage,
            dispatch_summaries_fn=None,
            generate_summaries_fn=gen_fn,
            message_processor=mock_processor,
        )

        await mon._expire_session("s1")

        gen_fn.assert_awaited_once_with("s1")
        assert storage.status_updates == [("s1", "expired")]

    @pytest.mark.asyncio
    async def test_no_summary_fn_still_expires(self, mock_processor):
        """If neither summary function is available, session is still expired."""
        storage = _RecordingStorage()
        mon = SessionLivenessMonitor(
            session_storage=storage,
            dispatch_summaries_fn=None,
            generate_summaries_fn=None,
            message_processor=mock_processor,
        )

        await mon._expire_session("s1")

        assert storage.status_updates == [("s1", "expired")]


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
        mock_result.stdout = "%5\t0\n%6\t0\n%7\t1\n"
        with patch("subprocess.run", return_value=mock_result):
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6") is True

    def test_dead_pane(self):
        """Pane ID not in tmux output means pane is dead."""
        mock_result = MagicMock()
        mock_result.stdout = "%5\t0\n%7\t0\n"
        with patch("subprocess.run", return_value=mock_result):
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6") is False

    def test_pane_dead_marker_is_not_alive(self):
        """pane_dead=1 excludes panes that tmux still lists after process exit."""
        mock_result = MagicMock()
        mock_result.stdout = "%6\t1\n"
        with patch("subprocess.run", return_value=mock_result):
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6") is False

    def test_alive_pane_with_socket_path(self):
        """When a socket path is known, liveness checks that exact tmux server."""
        mock_result = MagicMock()
        mock_result.stdout = "%6\t0\n"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6", "/tmp/tmux-1000/gobby") is True

        mock_run.assert_called_once_with(
            [
                "tmux",
                "-S",
                "/tmp/tmux-1000/gobby",
                "list-panes",
                "-a",
                "-F",
                "#{pane_id}\t#{pane_dead}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_dead_pane_checks_default_and_gobby_socket_when_path_unknown(self):
        """Legacy rows without a socket path check both the default server and Gobby's socket."""
        default_result = MagicMock()
        default_result.stdout = ""
        gobby_result = MagicMock()
        gobby_result.stdout = "%6\t0\n"

        with patch("subprocess.run", side_effect=[default_result, gobby_result]) as mock_run:
            assert SessionLivenessMonitor._is_tmux_pane_alive("%6") is True

        assert mock_run.call_args_list[0].args[0] == [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{pane_dead}",
        ]
        assert mock_run.call_args_list[1].args[0] == [
            "tmux",
            "-L",
            "gobby",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{pane_dead}",
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


class TestGetActiveTerminalSessions:
    """Tests for _get_active_terminal_sessions query."""

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

        result = monitor._get_active_terminal_sessions()

        assert _as_tuples(result) == [
            ("s1", 12345, None, None),
            ("s2", 67890, "%3", "/tmp/tmux-1000/gobby"),
        ]

    def test_skips_missing_pid(self, monitor, mock_session_storage):
        """Sessions without any terminal liveness metadata are excluded."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({})},
        ]

        result = monitor._get_active_terminal_sessions()

        assert result == []

    def test_includes_tmux_pane_without_pid(self, monitor, mock_session_storage):
        """Sessions with only tmux_pane are still sweepable."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"tmux_pane": "%1"})},
        ]

        result = monitor._get_active_terminal_sessions()

        assert _as_tuples(result) == [("s1", None, "%1", None)]

    def test_skips_invalid_pid(self, monitor, mock_session_storage):
        """Non-integer or zero/negative PIDs are excluded."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"parent_pid": "not-a-pid"})},
            {"id": "s2", "terminal_context": json.dumps({"parent_pid": 0})},
            {"id": "s3", "terminal_context": json.dumps({"parent_pid": -1})},
        ]

        result = monitor._get_active_terminal_sessions()

        assert result == []

    def test_accepts_numeric_string_pid(self, monitor, mock_session_storage):
        """Numeric parent_pid strings from older terminal contexts are usable."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"parent_pid": "123"})},
        ]

        result = monitor._get_active_terminal_sessions()

        assert _as_tuples(result) == [("s1", 123, None, None)]

    def test_handles_db_error(self, monitor, mock_session_storage):
        """DB errors return empty list."""
        mock_session_storage.db.fetchall.side_effect = Exception("DB error")

        result = monitor._get_active_terminal_sessions()

        assert result == []

    def test_excludes_only_active_agent_runs(self, monitor, mock_session_storage):
        """Query leaves completed agent sessions sweepable."""
        mock_session_storage.db.fetchall.return_value = []

        monitor._get_active_terminal_sessions()

        call_args = mock_session_storage.db.fetchall.call_args
        sql = call_args[0][0]
        assert "LEFT JOIN agent_runs" in sql
        assert "ar.status NOT IN ('running', 'pending')" in sql

    def test_non_string_tmux_pane_treated_as_none(self, monitor, mock_session_storage):
        """Non-string tmux_pane values are normalized to None."""
        mock_session_storage.db.fetchall.return_value = [
            {"id": "s1", "terminal_context": json.dumps({"parent_pid": 123, "tmux_pane": 42})},
        ]

        result = monitor._get_active_terminal_sessions()

        assert _as_tuples(result) == [("s1", 123, None, None)]

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

        result = monitor._get_active_terminal_sessions()

        assert _as_tuples(result) == [("s1", 123, "%4", None)]
