"""Tests for gobby.sessions.liveness_monitor module.

Tests for the SessionLivenessMonitor that detects dead CLI sessions
via parent PID checks and triggers session expiry + summary generation.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.config.tmux import TmuxConfig
from gobby.sessions.liveness_monitor import (
    _LOG_SAMPLE_LIMIT,
    SessionLivenessMonitor,
    _TmuxLivenessInventory,
    _TmuxSocketIdentity,
)
from gobby.terminal_ownership import PaneOwnershipDecision

pytestmark = pytest.mark.unit


def _inventories(
    socket_path: str | None,
    *panes: str,
) -> dict[_TmuxSocketIdentity, _TmuxLivenessInventory]:
    window_id = "@1"
    active_pane = panes[0] if panes else None
    return {
        _TmuxSocketIdentity(socket_path, None): _TmuxLivenessInventory(
            live_windows={window_id} if panes else set(),
            live_panes=set(panes),
            window_by_pane=dict.fromkeys(panes, window_id),
            active_pane_by_window={window_id: active_pane} if active_pane else {},
            session_by_window={window_id: "work"} if panes else {},
        )
    }


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

    def expire_if_active(self, session_id: str) -> None:
        if self.update_error is not None:
            raise self.update_error
        self.status_updates.append((session_id, "expired"))


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
        mock_session_storage.expire_if_active.assert_called_once_with("s1")
        assert "s1" in monitor._recently_handled

    @pytest.mark.asyncio
    async def test_offloads_db_tmux_and_touch_work(
        self,
        monitor,
        mock_session_storage,
        monkeypatch,
    ):
        record = SimpleNamespace(
            session_id="sess-tmux",
            source="claude",
            parent_pid=None,
            tmux_pane="%1",
            tmux_socket_path=None,
        )

        def get_active_terminal_sessions():
            return [record]

        def get_tmux_inventories_by_socket(records):
            assert records == [record]
            return _inventories(None, "%1")

        monkeypatch.setattr(monitor, "_get_active_terminal_sessions", get_active_terminal_sessions)
        monkeypatch.setattr(
            monitor,
            "_get_tmux_inventories_by_socket",
            get_tmux_inventories_by_socket,
        )

        async def run_in_place(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch(
            "gobby.sessions.liveness_monitor.asyncio.to_thread",
            new=AsyncMock(side_effect=run_in_place),
        ) as to_thread:
            await monitor._check_sessions()

        assert to_thread.await_args_list == [
            call(get_active_terminal_sessions),
            call(get_tmux_inventories_by_socket, [record]),
            call(mock_session_storage.touch, "sess-tmux"),
        ]
        mock_session_storage.touch.assert_called_once_with("sess-tmux")

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
        mock_session_storage.expire_if_active.assert_called_once_with("dead")
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
                "_get_tmux_inventories_by_socket",
                return_value=_inventories(None, "%6"),
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
                "_get_tmux_inventories_by_socket",
                return_value=_inventories(None, "%6"),
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
                "_get_tmux_inventories_by_socket",
                return_value=_inventories(None),
            ),
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("s1", False, None)
        mock_session_storage.expire_if_active.assert_called_once_with("s1")
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
                "_get_tmux_inventories_by_socket",
                return_value=_inventories(None),
            ),
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("s1", False, None)
        mock_session_storage.expire_if_active.assert_called_once_with("s1")
        assert set(monitor._recently_handled) == {"s1"}

    @pytest.mark.asyncio
    async def test_expired_missing_tmux_panes_are_silent_and_not_reprocessed(
        self,
        monitor: SessionLivenessMonitor,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        records = [
            SimpleNamespace(
                session_id=f"expired-{index}",
                source="codex",
                status="expired",
                machine_id="machine",
                parent_pid=100 + index,
                tmux_pane=f"%{index}",
                tmux_socket_path="/tmp/tmux",
                terminal_context={
                    "parent_pid": 100 + index,
                    "tmux_pane": f"%{index}",
                    "tmux_socket_path": "/tmp/tmux",
                },
            )
            for index in range(3)
        ]
        monkeypatch.setattr(monitor, "_get_active_terminal_sessions", lambda: records)
        monkeypatch.setattr(
            monitor,
            "_get_tmux_inventories_by_socket",
            lambda _records: _inventories("/tmp/tmux"),
        )

        with (
            caplog.at_level("INFO", logger="gobby.sessions.liveness_monitor"),
            patch.object(monitor, "_expire_session", new=AsyncMock()) as expire,
        ):
            await monitor._check_sessions()
            await monitor._check_sessions()

        expire.assert_not_awaited()
        assert monitor._recently_handled == {}
        assert not [
            record
            for record in caplog.records
            if record.getMessage().startswith("Detected missing tmux pane")
        ]
        assert not [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "session_liveness_missing_panes_expired"
        ]

    @pytest.mark.asyncio
    async def test_active_missing_tmux_panes_emit_one_aggregate_event(
        self,
        monitor: SessionLivenessMonitor,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        records = [
            SimpleNamespace(
                session_id=f"active-{index}",
                source="codex",
                status="active",
                machine_id="machine",
                parent_pid=100 + index,
                tmux_pane=f"%{index}",
                tmux_socket_path="/tmp/tmux",
                terminal_context={
                    "parent_pid": 100 + index,
                    "tmux_pane": f"%{index}",
                    "tmux_socket_path": "/tmp/tmux",
                },
            )
            for index in range(_LOG_SAMPLE_LIMIT + 2)
        ]
        monkeypatch.setattr(monitor, "_get_active_terminal_sessions", lambda: records)
        monkeypatch.setattr(
            monitor,
            "_get_tmux_inventories_by_socket",
            lambda _records: _inventories("/tmp/tmux"),
        )

        with (
            caplog.at_level("INFO", logger="gobby.sessions.liveness_monitor"),
            patch.object(monitor, "_expire_session", new=AsyncMock()) as expire,
        ):
            await monitor._check_sessions()

        assert [call.args[0] for call in expire.await_args_list] == [
            f"active-{index}" for index in range(_LOG_SAMPLE_LIMIT + 2)
        ]
        events = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "session_liveness_missing_panes_expired"
        ]
        assert len(events) == 1
        assert getattr(events[0], "session_count", None) == _LOG_SAMPLE_LIMIT + 2
        assert getattr(events[0], "sample_session_ids", None) == tuple(
            f"active-{index}" for index in range(_LOG_SAMPLE_LIMIT)
        )
        assert getattr(events[0], "sample_tmux_panes", None) == tuple(
            f"%{index}" for index in range(_LOG_SAMPLE_LIMIT)
        )

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
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="tmux error"
                ),
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
                "source": "claude",
                "terminal_context": json.dumps({"parent_pid": 99999}),
            },
        ]

        with patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=False):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("s1", False, None)
        mock_session_storage.expire_if_active.assert_called_once_with("s1")
        assert set(monitor._recently_handled) == {"s1"}

    @pytest.mark.asyncio
    async def test_dead_pid_no_tmux_pane_does_not_expire_codex(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        """Codex parent-PID-only rows are weak liveness records."""
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "s1",
                "source": "codex",
                "terminal_context": json.dumps({"parent_pid": 99999}),
            },
        ]

        with patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=False):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()
        mock_session_storage.update_status.assert_not_called()
        mock_session_storage.touch.assert_not_called()
        assert monitor._recently_handled == {}

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
            "_get_tmux_inventories_by_socket",
            return_value=_inventories(None, "%6"),
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
            "_get_tmux_inventories_by_socket",
            return_value=_inventories(None),
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("s1", False, None)
        mock_session_storage.expire_if_active.assert_called_once_with("s1")
        mock_session_storage.touch.assert_not_called()
        assert set(monitor._recently_handled) == {"s1"}

    @pytest.mark.asyncio
    async def test_dead_nested_child_expires_when_live_expired_parent_owns_pane(
        self,
        monitor,
        monkeypatch,
    ):
        terminal_context = {
            "tmux_pane": "%226",
            "tmux_socket_path": "/tmp/tmux-501/gobby",
        }
        parent = SimpleNamespace(
            session_id="codex-parent",
            source="codex",
            status="expired",
            machine_id="machine",
            parent_pid=100,
            tmux_pane="%226",
            tmux_socket_path="/tmp/tmux-501/gobby",
            terminal_context={**terminal_context, "parent_pid": 100},
        )
        child = SimpleNamespace(
            session_id="grok-child",
            source="grok",
            status="paused",
            machine_id="machine",
            parent_pid=200,
            tmux_pane="%226",
            tmux_socket_path="/tmp/tmux-501/gobby",
            terminal_context={**terminal_context, "parent_pid": 200},
        )
        identity = ("machine", "tmux_socket_path:/tmp/tmux-501/gobby", "%226")
        decision = PaneOwnershipDecision(
            identity=identity,
            requested_session_id="grok-child",
            owner=parent,
            reason="validated_live_process",
            validated_session_ids=frozenset({"codex-parent"}),
        )
        monkeypatch.setattr(
            monitor,
            "_get_active_terminal_sessions",
            lambda: [child, parent],
        )
        monkeypatch.setattr(
            monitor,
            "_get_tmux_inventories_by_socket",
            lambda _records: _inventories("/tmp/tmux-501/gobby", "%226"),
        )

        with (
            patch(
                "gobby.sessions.liveness_monitor.resolve_pane_ownership",
                return_value=decision,
            ) as resolve_ownership,
            patch.object(monitor, "_expire_session", new=AsyncMock()) as expire,
        ):
            await monitor._check_sessions()

        assert resolve_ownership.call_args == call(
            [child, parent], requested_session_id="grok-child"
        )
        assert resolve_ownership.call_count == 1
        assert expire.await_args == call("grok-child")
        assert expire.await_count == 1
        assert set(monitor._recently_handled) == {"grok-child"}

    @pytest.mark.asyncio
    async def test_interactive_window_survives_pane_replacement(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        monitor._tmux_config = TmuxConfig(socket_name="spawn")
        context = {
            "tmux_socket_path": "/tmp/tmux-501/default",
            "tmux_window_id": "@7",
            "tmux_pane": "%6",
            "tmux_session": "work",
        }
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "interactive",
                "status": "paused",
                "machine_id": "machine",
                "terminal_context": context,
            }
        ]
        mock_session_storage.revive_expired_terminal_session.return_value = SimpleNamespace(
            status="paused"
        )
        inventory = _TmuxLivenessInventory(
            live_windows={"@7"},
            live_panes={"%9"},
            window_by_pane={"%9": "@7"},
            active_pane_by_window={"@7": "%9"},
            session_by_window={"@7": "work"},
        )

        with patch.object(
            monitor,
            "_get_tmux_inventories_by_socket",
            return_value={_TmuxSocketIdentity("/tmp/tmux-501/default", None): inventory},
        ):
            await monitor._check_sessions()

        mock_session_storage.update.assert_called_once_with(
            "interactive",
            terminal_context={"tmux_pane": "%9"},
        )
        mock_session_storage.touch.assert_called_once_with("interactive")
        mock_dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_interactive_window_removal_expires_once(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        monitor._tmux_config = TmuxConfig(socket_name="spawn")
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "interactive",
                "status": "paused",
                "terminal_context": {
                    "parent_pid": 123,
                    "tmux_socket_path": "/tmp/tmux-501/default",
                    "tmux_window_id": "@7",
                    "tmux_pane": "%6",
                },
            }
        ]

        with (
            patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=True),
            patch.object(
                monitor,
                "_get_tmux_inventories_by_socket",
                return_value={
                    _TmuxSocketIdentity(
                        "/tmp/tmux-501/default", None
                    ): SessionLivenessMonitor._empty_tmux_inventory()
                },
            ),
        ):
            await monitor._check_sessions()
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("interactive", False, None)
        mock_session_storage.expire_if_active.assert_called_once_with("interactive")

    @pytest.mark.asyncio
    async def test_interactive_probe_failure_fails_open_with_dead_pid(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        monitor._tmux_config = TmuxConfig(socket_name="spawn")
        socket = _TmuxSocketIdentity("/tmp/tmux-501/default", None)
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "interactive",
                "status": "paused",
                "terminal_context": {
                    "parent_pid": 99999,
                    "tmux_socket_path": socket.socket_path,
                    "tmux_window_id": "@7",
                    "tmux_pane": "%6",
                },
            }
        ]

        with (
            patch.object(SessionLivenessMonitor, "_is_pid_alive", return_value=False),
            patch.object(
                monitor,
                "_get_tmux_inventories_by_socket",
                return_value={socket: None},
            ),
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_not_called()
        mock_session_storage.expire_if_active.assert_not_called()

    @pytest.mark.asyncio
    async def test_legacy_interactive_pane_backfills_window_identity(
        self, monitor, mock_session_storage
    ):
        monitor._tmux_config = TmuxConfig(socket_name="spawn")
        socket = _TmuxSocketIdentity("/tmp/tmux-501/default", None)
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "legacy",
                "status": "paused",
                "machine_id": "machine",
                "terminal_context": {
                    "tmux_socket_path": socket.socket_path,
                    "tmux_pane": "%6",
                },
            }
        ]
        mock_session_storage.revive_expired_terminal_session.return_value = SimpleNamespace(
            status="paused"
        )
        inventory = _TmuxLivenessInventory(
            live_windows={"@7"},
            live_panes={"%6"},
            window_by_pane={"%6": "@7"},
            active_pane_by_window={"@7": "%6"},
            session_by_window={"@7": "work"},
        )

        with patch.object(
            monitor,
            "_get_tmux_inventories_by_socket",
            return_value={socket: inventory},
        ):
            await monitor._check_sessions()

        mock_session_storage.update.assert_called_once_with(
            "legacy",
            terminal_context={"tmux_window_id": "@7", "tmux_session": "work"},
        )
        mock_session_storage.touch.assert_called_once_with("legacy")

    @pytest.mark.asyncio
    async def test_live_interactive_window_revives_false_expiry_as_paused(
        self, monitor, mock_session_storage
    ):
        monitor._tmux_config = TmuxConfig(socket_name="spawn")
        socket = _TmuxSocketIdentity("/tmp/tmux-501/default", None)
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "expired-interactive",
                "status": "expired",
                "machine_id": "machine",
                "terminal_context": {
                    "tmux_socket_path": socket.socket_path,
                    "tmux_window_id": "@7",
                    "tmux_pane": "%6",
                },
            }
        ]
        mock_session_storage.revive_expired_terminal_session.return_value = SimpleNamespace(
            status="active"
        )
        inventory = _TmuxLivenessInventory(
            live_windows={"@7"},
            live_panes={"%6"},
            window_by_pane={"%6": "@7"},
            active_pane_by_window={"@7": "%6"},
            session_by_window={"@7": "work"},
        )

        with patch.object(
            monitor,
            "_get_tmux_inventories_by_socket",
            return_value={socket: inventory},
        ):
            await monitor._check_sessions()

        mock_session_storage.revive_expired_terminal_session.assert_called_once_with(
            "expired-interactive"
        )
        mock_session_storage.update_status.assert_called_once_with("expired-interactive", "paused")

    @pytest.mark.asyncio
    async def test_configured_spawn_socket_keeps_pane_lifecycle(
        self, monitor, mock_session_storage, mock_dispatch_fn
    ):
        monitor._tmux_config = TmuxConfig(socket_name="spawn")
        socket = _TmuxSocketIdentity("/tmp/tmux-501/spawn", None)
        mock_session_storage.db.fetchall.return_value = [
            {
                "id": "spawned",
                "status": "paused",
                "terminal_context": {
                    "tmux_socket_path": socket.socket_path,
                    "tmux_window_id": "@7",
                    "tmux_pane": "%6",
                },
            }
        ]
        inventory = _TmuxLivenessInventory(
            live_windows={"@7"},
            live_panes={"%9"},
            window_by_pane={"%9": "@7"},
            active_pane_by_window={"@7": "%9"},
            session_by_window={"@7": "spawned"},
        )

        with patch.object(
            monitor,
            "_get_tmux_inventories_by_socket",
            return_value={socket: inventory},
        ):
            await monitor._check_sessions()

        mock_dispatch_fn.assert_called_once_with("spawned", False, None)
        mock_session_storage.expire_if_active.assert_called_once_with("spawned")


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


class TestTmuxInventory:
    def test_parses_live_windows_panes_and_active_mapping(self):
        inventory = SessionLivenessMonitor._parse_tmux_inventory(
            "work\t@7\t%5\t0\t0\nwork\t@7\t%6\t1\t0\ndead\t@8\t%7\t1\t1\n"
        )

        assert inventory.live_windows == {"@7"}
        assert inventory.live_panes == {"%5", "%6"}
        assert inventory.window_by_pane == {"%5": "@7", "%6": "@7"}
        assert inventory.active_pane_by_window == {"@7": "%6"}
        assert inventory.session_by_window == {"@7": "work"}

    def test_exact_socket_path_uses_configured_command(self, monitor):
        monitor._tmux_config = TmuxConfig(command="custom-tmux", socket_name="spawn")
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="work\t@7\t%6\t1\t0\n",
            stderr="",
        )
        socket = _TmuxSocketIdentity("/tmp/user-tmux", None)

        with patch("subprocess.run", return_value=result) as mock_run:
            inventory = monitor._list_tmux_inventory(socket)

        assert inventory is not None
        assert inventory.live_panes == {"%6"}
        assert mock_run.call_args.args[0][:3] == ["custom-tmux", "-S", "/tmp/user-tmux"]

    @pytest.mark.parametrize(
        "error",
        [
            FileNotFoundError(),
            subprocess.TimeoutExpired("tmux", 5),
            OSError("tmux server not running"),
        ],
    )
    def test_probe_error_returns_none(self, monitor, error):
        with patch("subprocess.run", side_effect=error):
            assert monitor._list_tmux_inventory(_TmuxSocketIdentity(None, None)) is None

    def test_nonzero_exit_returns_probe_failure(self, monitor):
        result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="tmux error")
        with patch("subprocess.run", return_value=result):
            assert monitor._list_tmux_inventory(_TmuxSocketIdentity(None, None)) is None


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
                        "tmux_socket_name": "spawn",
                        "tmux_window_id": "@9",
                        "tmux_session": "work",
                    }
                ),
            },
        ]

        result = monitor._get_active_terminal_sessions()

        assert _as_tuples(result) == [
            ("s1", 12345, None, None),
            ("s2", 67890, "%3", "/tmp/tmux-1000/gobby"),
        ]
        assert result[1].tmux_socket_name == "spawn"
        assert result[1].tmux_window_id == "@9"
        assert result[1].tmux_session == "work"

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

    def test_non_string_tmux_socket_path_treated_as_none(
        self,
        monitor: SessionLivenessMonitor,
        mock_session_storage: MagicMock,
    ) -> None:
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
