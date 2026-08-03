from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.liveness_monitor import (
    SessionLivenessMonitor,
    _TerminalLivenessRecord,
    _TmuxLivenessInventory,
    _TmuxSocketIdentity,
)
from gobby.terminal_ownership import OwnershipReason, PaneOwnershipDecision


def _record(
    session_id: str,
    *,
    status: str = "active",
    pid: int = 10,
    pane: str | None = "%1",
    window: str | None = "@1",
) -> _TerminalLivenessRecord:
    context: dict[str, Any] = {
        "parent_pid": pid,
        "parent_create_time": float(pid),
        "tmux_socket_name": "gobby",
        "tty": "/dev/ttys001",
    }
    if pane is not None:
        context["tmux_pane"] = pane
    if window is not None:
        context["tmux_window_id"] = window
    return _TerminalLivenessRecord(
        session_id=session_id,
        source="codex",
        parent_pid=pid,
        tmux_pane=pane,
        tmux_socket_path=None,
        tmux_socket_name="gobby",
        tmux_window_id=window,
        status=status,
        machine_id="21000000-0000-4000-8000-000000000003",
        terminal_context=context,
    )


def _inventory(
    *,
    pane: str = "%1",
    window: str = "@1",
) -> _TmuxLivenessInventory:
    return _TmuxLivenessInventory(
        live_windows={window},
        live_panes={pane},
        window_by_pane={pane: window},
        active_pane_by_window={window: pane},
        session_by_window={window: "work"},
    )


class _Storage:
    def __init__(self, expire_result: object | None = None) -> None:
        self.db = MagicMock()
        self.expire_result = expire_result
        self.expire_calls: list[str] = []
        self.update = MagicMock()

    def expire_if_active(self, session_id: str) -> object | None:
        self.expire_calls.append(session_id)
        return self.expire_result


class _Processor:
    def __init__(self) -> None:
        self.unregistered: list[str] = []

    def unregister_session(self, session_id: str) -> None:
        self.unregistered.append(session_id)


@pytest.fixture
def storage() -> _Storage:
    return _Storage(expire_result=SimpleNamespace(status="expired"))


@pytest.fixture
def monitor(storage: _Storage) -> SessionLivenessMonitor:
    return SessionLivenessMonitor(session_storage=cast(Any, storage), poll_interval=0.01)


class TestPaneOwnershipLifecycle:
    @pytest.mark.asyncio
    async def test_ownerless_group_preserves_sessions_and_title(
        self,
        monitor: SessionLivenessMonitor,
    ) -> None:
        first = _record("first")
        second = _record("second", pid=20)
        decision = PaneOwnershipDecision(
            identity=("21000000-0000-4000-8000-000000000003", "tmux_socket_name:gobby", "%1"),
            requested_session_id="first",
            owner=None,
            reason="ownerless",
        )

        with (
            patch(
                "gobby.sessions.liveness_monitor.resolve_pane_ownership",
                return_value=decision,
            ),
            patch.object(
                monitor,
                "_expire_session",
                new=AsyncMock(return_value=True),
            ) as expire,
            patch.object(
                monitor,
                "_release_tmux_title",
                new=AsyncMock(),
            ) as release,
        ):
            await monitor._handle_live_pane_group(
                [first, second],
                100.0,
                inventory=_inventory(),
            )

        expire.assert_not_awaited()
        release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_background_peer_is_preserved_with_foreground_owner(
        self,
        monitor: SessionLivenessMonitor,
    ) -> None:
        owner = _record("owner")
        background = _record("background", pid=20)
        decision = PaneOwnershipDecision(
            identity=("21000000-0000-4000-8000-000000000003", "tmux_socket_name:gobby", "%1"),
            requested_session_id="owner",
            owner=owner,
            reason="validated_foreground_process",
            validated_session_ids=frozenset({"owner"}),
        )

        with (
            patch(
                "gobby.sessions.liveness_monitor.resolve_pane_ownership",
                return_value=decision,
            ),
            patch.object(
                monitor,
                "_expire_session",
                new=AsyncMock(return_value=True),
            ) as expire,
            patch.object(
                monitor,
                "_repair_tmux_target",
                new=AsyncMock(return_value=owner),
            ) as repair,
            patch.object(
                monitor,
                "_release_tmux_title",
                new=AsyncMock(),
            ) as release,
        ):
            await monitor._handle_live_pane_group(
                [owner, background],
                100.0,
                inventory=_inventory(),
            )

        expire.assert_not_awaited()
        repair.assert_awaited_once_with(owner, _inventory())
        release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nested_foreground_group_keeps_both_rows_and_repairs_owner(
        self,
        monitor: SessionLivenessMonitor,
    ) -> None:
        outer = _record("outer")
        inner = _record("inner", pid=20)
        decision = PaneOwnershipDecision(
            identity=("21000000-0000-4000-8000-000000000003", "tmux_socket_name:gobby", "%1"),
            requested_session_id="outer",
            owner=outer,
            reason="nested_outermost_process",
            validated_session_ids=frozenset({"outer", "inner"}),
        )

        with (
            patch(
                "gobby.sessions.liveness_monitor.resolve_pane_ownership",
                return_value=decision,
            ),
            patch.object(
                monitor,
                "_expire_session",
                new=AsyncMock(return_value=True),
            ) as expire,
            patch.object(
                monitor,
                "_repair_tmux_target",
                new=AsyncMock(return_value=outer),
            ) as repair,
        ):
            await monitor._handle_live_pane_group(
                [outer, inner],
                100.0,
                inventory=_inventory(),
            )

        expire.assert_not_awaited()
        repair.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handoff_ready_ownerless_row_is_preserved(
        self,
        monitor: SessionLivenessMonitor,
    ) -> None:
        handoff = _record("handoff", status="handoff_ready")
        decision = PaneOwnershipDecision(
            identity=("21000000-0000-4000-8000-000000000003", "tmux_socket_name:gobby", "%1"),
            requested_session_id="handoff",
            owner=None,
            reason="ownerless",
        )

        with (
            patch(
                "gobby.sessions.liveness_monitor.resolve_pane_ownership",
                return_value=decision,
            ),
            patch.object(
                monitor,
                "_expire_session",
                new=AsyncMock(return_value=True),
            ) as expire,
            patch.object(
                monitor,
                "_release_tmux_title",
                new=AsyncMock(),
            ),
        ):
            await monitor._handle_live_pane_group(
                [handoff],
                100.0,
                inventory=_inventory(),
            )

        expire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_target_without_group_identity_skips_process_expiry(
        self,
        monitor: SessionLivenessMonitor,
    ) -> None:
        record = _record("missing-socket")
        assert record.terminal_context is not None
        record.terminal_context.pop("tmux_socket_name")
        inventory = _inventory()

        with (
            patch.object(
                monitor,
                "_get_active_terminal_sessions",
                return_value=[record],
            ),
            patch.object(
                monitor,
                "_get_tmux_inventories_by_socket",
                return_value={monitor._socket_identity(record): inventory},
            ),
            patch(
                "gobby.sessions.liveness_monitor.inspect_foreground_ownership",
            ) as inspect,
            patch.object(
                monitor,
                "_expire_session",
                new=AsyncMock(return_value=True),
            ) as expire,
            patch.object(
                monitor,
                "_release_tmux_title",
                new=AsyncMock(),
            ) as release,
        ):
            await monitor._check_sessions()

        inspect.assert_not_called()
        expire.assert_not_awaited()
        release.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason",
        ["process_inspection_error", "ambiguous_foreground_processes"],
    )
    async def test_indeterminate_group_preserves_lifecycle_and_title(
        self,
        monitor: SessionLivenessMonitor,
        reason: OwnershipReason,
    ) -> None:
        record = _record("session")
        decision = PaneOwnershipDecision(
            identity=("21000000-0000-4000-8000-000000000003", "tmux_socket_name:gobby", "%1"),
            requested_session_id="session",
            owner=None,
            reason=reason,
        )

        with (
            patch(
                "gobby.sessions.liveness_monitor.resolve_pane_ownership",
                return_value=decision,
            ),
            patch.object(
                monitor,
                "_expire_session",
                new=AsyncMock(return_value=True),
            ) as expire,
            patch.object(
                monitor,
                "_repair_tmux_target",
                new=AsyncMock(),
            ) as repair,
            patch.object(
                monitor,
                "_release_tmux_title",
                new=AsyncMock(),
            ) as release,
        ):
            await monitor._handle_live_pane_group(
                [record],
                100.0,
                inventory=_inventory(),
            )

        expire.assert_not_awaited()
        repair.assert_not_awaited()
        release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_target_expires_session_then_releases_title(
        self,
        monitor: SessionLivenessMonitor,
    ) -> None:
        owner = _record("owner")
        socket = monitor._socket_identity(owner)

        with (
            patch.object(
                monitor,
                "_get_active_terminal_sessions",
                return_value=[owner],
            ),
            patch.object(
                monitor,
                "_get_tmux_inventories_by_socket",
                return_value={socket: _TmuxLivenessInventory(set(), set(), {}, {}, {})},
            ),
            patch.object(
                monitor,
                "_expire_session",
                new=AsyncMock(return_value=True),
            ) as expire,
            patch.object(
                monitor,
                "_release_tmux_title",
                new=AsyncMock(),
            ) as release,
        ):
            await monitor._check_sessions()

        expire.assert_awaited_once_with("owner")
        release.assert_awaited_once_with(owner)

    @pytest.mark.asyncio
    async def test_tmux_probe_failure_preserves_state(
        self,
        monitor: SessionLivenessMonitor,
    ) -> None:
        record = _record("session")
        socket = monitor._socket_identity(record)

        with (
            patch.object(
                monitor,
                "_get_active_terminal_sessions",
                return_value=[record],
            ),
            patch.object(
                monitor,
                "_get_tmux_inventories_by_socket",
                return_value={socket: None},
            ),
            patch.object(
                monitor,
                "_expire_session",
                new=AsyncMock(return_value=True),
            ) as expire,
            patch(
                "gobby.sessions.liveness_monitor.resolve_pane_ownership",
            ) as resolve,
        ):
            await monitor._check_sessions()

        expire.assert_not_awaited()
        resolve.assert_not_called()


class TestTmuxTargetRepair:
    @pytest.mark.asyncio
    async def test_repairs_pane_only_after_caller_proves_ownership(
        self,
        monitor: SessionLivenessMonitor,
        storage: _Storage,
    ) -> None:
        record = _record("session", pane="%1", window="@1")
        inventory = _inventory(pane="%9", window="@1")

        repaired = await monitor._repair_tmux_target(record, inventory)

        assert repaired is not None
        assert repaired.tmux_pane == "%9"
        storage.update.assert_called_once_with(
            "session",
            terminal_context={"tmux_pane": "%9", "tmux_session": "work"},
        )

    @pytest.mark.asyncio
    async def test_returns_none_when_window_and_pane_are_missing(
        self,
        monitor: SessionLivenessMonitor,
    ) -> None:
        repaired = await monitor._repair_tmux_target(
            _record("session"),
            _TmuxLivenessInventory(set(), set(), {}, {}, {}),
        )

        assert repaired is None


class TestConditionalExpiry:
    @pytest.mark.asyncio
    async def test_success_dispatches_summary_and_unregisters(self) -> None:
        storage = _Storage(expire_result=SimpleNamespace(status="expired"))
        dispatch = MagicMock()
        processor = _Processor()
        monitor = SessionLivenessMonitor(
            session_storage=cast(Any, storage),
            dispatch_summaries_fn=dispatch,
            message_processor=processor,
        )

        result = await monitor._expire_session("session")

        assert result is True
        assert storage.expire_calls == ["session"]
        dispatch.assert_called_once_with("session", False, None)
        assert processor.unregistered == ["session"]

    @pytest.mark.asyncio
    async def test_status_race_skips_summary_and_cleanup(self) -> None:
        storage = _Storage(expire_result=None)
        dispatch = MagicMock()
        processor = _Processor()
        monitor = SessionLivenessMonitor(
            session_storage=cast(Any, storage),
            dispatch_summaries_fn=dispatch,
            message_processor=processor,
        )

        result = await monitor._expire_session("session")

        assert result is False
        dispatch.assert_not_called()
        assert processor.unregistered == []

    @pytest.mark.asyncio
    async def test_generate_summary_fallback_runs_after_expiry(self) -> None:
        storage = _Storage(expire_result=SimpleNamespace(status="expired"))
        generate = AsyncMock()
        monitor = SessionLivenessMonitor(
            session_storage=cast(Any, storage),
            generate_summaries_fn=generate,
        )

        result = await monitor._expire_session("session")

        assert result is True
        generate.assert_awaited_once_with("session")


class TestTmuxInventory:
    def test_parses_live_windows_panes_and_active_mapping(self) -> None:
        output = "work\t@1\t%1\t1\t0\nwork\t@1\t%2\t0\t0\nother\t@2\t%3\t1\t0\n"

        inventory = SessionLivenessMonitor._parse_tmux_inventory(output)

        assert inventory.live_windows == {"@1", "@2"}
        assert inventory.live_panes == {"%1", "%2", "%3"}
        assert inventory.window_by_pane == {"%1": "@1", "%2": "@1", "%3": "@2"}
        assert inventory.active_pane_by_window == {"@1": "%1", "@2": "%3"}
        assert inventory.session_by_window == {"@1": "work", "@2": "other"}

    def test_exact_socket_path_uses_configured_command(
        self, monitor: SessionLivenessMonitor
    ) -> None:
        socket = _TmuxSocketIdentity("/tmp/tmux-501/default", None)

        assert monitor._tmux_commands_for_socket(socket) == [
            ["tmux", "-S", "/tmp/tmux-501/default"]
        ]

    def test_probe_error_returns_none(self, monitor: SessionLivenessMonitor) -> None:
        with patch("subprocess.run", side_effect=OSError("tmux unavailable")):
            inventory = monitor._list_tmux_inventory(_TmuxSocketIdentity(None, "gobby"))

        assert inventory is None


class TestGetActiveTerminalSessions:
    def test_uses_central_eligible_statuses_and_parses_context(self, storage: _Storage) -> None:
        storage.db.fetchall.return_value = [
            {
                "id": "session",
                "source": "codex",
                "status": "handoff_ready",
                "machine_id": "21000000-0000-4000-8000-000000000003",
                "terminal_context": json.dumps(
                    {
                        "parent_pid": "42",
                        "parent_create_time": 10.0,
                        "tmux_pane": "%1",
                        "tmux_socket_name": "gobby",
                        "tty": "/dev/ttys001",
                    }
                ),
            }
        ]
        monitor = SessionLivenessMonitor(session_storage=cast(Any, storage))

        records = monitor._get_active_terminal_sessions()

        assert len(records) == 1
        assert records[0].status == "handoff_ready"
        assert records[0].parent_pid == 42
        query = storage.db.fetchall.call_args.args[0]
        assert "s.status = ANY(%s)" in query
        assert storage.db.fetchall.call_args.args[1] == (["active", "paused", "handoff_ready"],)

    def test_skips_context_without_process_or_tmux_identity(self, storage: _Storage) -> None:
        storage.db.fetchall.return_value = [
            {
                "id": "session",
                "terminal_context": {"tty": "/dev/ttys001"},
            }
        ]
        monitor = SessionLivenessMonitor(session_storage=cast(Any, storage))

        assert monitor._get_active_terminal_sessions() == []

    def test_db_failure_fails_open(self, storage: _Storage) -> None:
        storage.db.fetchall.side_effect = RuntimeError("database unavailable")
        monitor = SessionLivenessMonitor(session_storage=cast(Any, storage))

        assert monitor._get_active_terminal_sessions() == []

    def test_pool_outage_logs_throttled_warning(
        self,
        storage: _Storage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        from psycopg_pool import PoolTimeout

        import gobby.sessions.liveness_monitor as liveness_module

        liveness_module._pool_outage_log._last_logged.clear()
        storage.db.fetchall.side_effect = PoolTimeout("couldn't get a connection after 5.00 sec")
        monitor = SessionLivenessMonitor(session_storage=cast(Any, storage))

        with caplog.at_level(logging.DEBUG, logger="gobby.sessions.liveness_monitor"):
            assert monitor._get_active_terminal_sessions() == []
            assert monitor._get_active_terminal_sessions() == []

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "hub temporarily unavailable; skipping pass" in warnings[0].getMessage()
        assert warnings[0].exc_info is None
