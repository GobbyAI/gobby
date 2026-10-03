from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.tmux.session_manager import TmuxPaneInfo
from gobby.sessions import liveness_monitor as liveness_mod
from gobby.sessions.liveness_monitor import (
    SessionLivenessMonitor,
    _TerminalLivenessRecord,
)
from gobby.sessions.processor import SessionMessageProcessor
from gobby.terminal_ownership import OwnershipState

_LOCAL = "21000000-0000-4000-8000-000000000003"
_REMOTE = "21000000-0000-4000-8000-000000000004"
_OBSERVED = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(liveness_mod, "get_machine_id", lambda: _LOCAL)


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
        "tty": "/dev/ttys001",
    }
    if pane is not None:
        context["tmux_pane"] = pane
    if window is not None:
        context["tmux_window_id"] = window
    return _TerminalLivenessRecord(
        session_id=session_id,
        parent_pid=pid,
        tmux_pane=pane,
        tmux_window_id=window,
        status=status,
        machine_id=_LOCAL,
        terminal_context=context,
        updated_at=_OBSERVED,
    )


class _Storage:
    def __init__(self, expire_result: object | None = None) -> None:
        self.db = MagicMock()
        self.expire_result = expire_result
        self.expire_calls: list[str] = []
        self.expire_snapshots: list[tuple[str, str, datetime]] = []
        self.guarded_expire_calls: list[tuple[str, str, str, datetime]] = []
        self.live_host_epochs: list[str | None] = []
        self.update = MagicMock()

    def expire_if_active(
        self, session_id: str, *, machine_id: str, observed_updated_at: datetime
    ) -> object | None:
        self.expire_calls.append(session_id)
        self.expire_snapshots.append((session_id, machine_id, observed_updated_at))
        return self.expire_result

    def expire_if_paused_terminal_exited(
        self,
        session_id: str,
        *,
        terminal_id: str,
        machine_id: str,
        observed_updated_at: datetime,
        live_host_epoch: str | None,
    ) -> object | None:
        self.guarded_expire_calls.append((session_id, terminal_id, machine_id, observed_updated_at))
        self.live_host_epochs.append(live_host_epoch)
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


_SOCKET = "/tmp/hand-started-tmux.sock"


def _pane(
    pane_id: str,
    window_id: str,
    *,
    dead: bool = False,
    server_pid: int = 100,
) -> TmuxPaneInfo:
    return TmuxPaneInfo(
        socket_path=_SOCKET,
        server_pid=server_pid,
        server_start_time=200,
        session_name="work",
        window_id=window_id,
        window_name=None,
        pane_id=pane_id,
        pane_pid=None,
        pane_title=None,
        pane_dead=dead,
        pane_command=None,
        pane_path=None,
    )


def _fake_tmux(monkeypatch: pytest.MonkeyPatch, panes: list[TmuxPaneInfo] | None) -> list[str]:
    """Answer every list-panes with ``panes``; return the sockets probed."""
    probed: list[str] = []

    class FakeTmux:
        def __init__(self, socket_path: str) -> None:
            probed.append(socket_path)

        async def list_panes(self, **_kwargs: Any) -> list[TmuxPaneInfo] | None:
            return panes

    monkeypatch.setattr(liveness_mod, "TmuxSessionManager", FakeTmux)
    return probed


def _tmux_record(session_id: str, **socket: object) -> _TerminalLivenessRecord:
    record = _record(session_id)
    context = dict(record.terminal_context or {})
    context.update(socket)
    return replace(record, terminal_context=context)


def _external_record(session_id: str) -> _TerminalLivenessRecord:
    return _tmux_record(
        session_id,
        tmux_socket_path=_SOCKET,
        tmux_server_pid=100,
        tmux_server_start_time=200,
    )


class TestTmuxTargetLiveness:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("panes", "expired"),
        [
            # The window was killed: its pane is gone from the server.
            ([_pane("%7", "@7")], True),
            # The pane is still listed but its process exited.
            ([_pane("%1", "@1", dead=True)], True),
            # No tmux server answers on the socket any more.
            ([], True),
            # A restarted server reused the pane id; it is a different pane.
            ([_pane("%1", "@1", server_pid=101)], True),
            ([_pane("%1", "@1")], False),
            # The pane was replaced inside the window the session lives in.
            ([_pane("%2", "@1")], False),
            # tmux failed without answering: keep the session as it is.
            (None, False),
        ],
    )
    async def test_dead_tmux_target_expires_its_session(
        self,
        panes: list[TmuxPaneInfo] | None,
        expired: bool,
        monitor: SessionLivenessMonitor,
        storage: _Storage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        probed = _fake_tmux(monkeypatch, panes)
        records = [_external_record("a"), _external_record("b")]
        monkeypatch.setattr(monitor, "_get_active_terminal_sessions", lambda: records)

        await monitor._check_sessions()

        assert storage.expire_calls == (["a", "b"] if expired else [])
        # One probe per socket per sweep, against the socket the session recorded.
        assert probed == [_SOCKET]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [TimeoutError("tmux hung"), FileNotFoundError("tmux")])
    async def test_failed_tmux_probe_keeps_the_sweep_going(
        self,
        error: Exception,
        monitor: SessionLivenessMonitor,
        storage: _Storage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class RaisingTmux:
            def __init__(self, _socket_path: str) -> None:
                pass

            async def list_panes(self, **_kwargs: Any) -> list[TmuxPaneInfo] | None:
                raise error

        monkeypatch.setattr(liveness_mod, "TmuxSessionManager", RaisingTmux)
        process = _record("process", pane=None, window=None)
        records = [_external_record("tmux"), process]
        monkeypatch.setattr(monitor, "_get_active_terminal_sessions", lambda: records)
        monkeypatch.setattr(
            liveness_mod,
            "inspect_foreground_ownership",
            lambda _record: SimpleNamespace(state=OwnershipState.OWNERLESS),
        )

        await monitor._check_sessions()

        # The tmux session is kept; the session after it is still judged.
        assert storage.expire_calls == ["process"]

    @pytest.mark.asyncio
    # A bare -L socket name addresses no server Gobby can reach (#22856).
    @pytest.mark.parametrize("socket", [{}, {"tmux_socket_name": "gobby"}], ids=["none", "named"])
    async def test_tmux_target_without_a_socket_is_left_alone(
        self,
        socket: dict[str, object],
        monitor: SessionLivenessMonitor,
        storage: _Storage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        probed = _fake_tmux(monkeypatch, [])
        record = _tmux_record("socketless", **socket)
        monkeypatch.setattr(monitor, "_get_active_terminal_sessions", lambda: [record])

        await monitor._check_sessions()

        assert storage.expire_calls == []
        assert probed == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("record_machine", "local_machine"),
        [(_REMOTE, _LOCAL), (None, _LOCAL), (_LOCAL, None)],
        ids=["remote-session", "session-machine-unknown", "local-machine-unknown"],
    )
    async def test_only_this_machines_sessions_are_probed(
        self,
        record_machine: str | None,
        local_machine: str | None,
        monitor: SessionLivenessMonitor,
        storage: _Storage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Neither another machine's socket nor its pids exist here.
        probed = _fake_tmux(monkeypatch, [])
        inspected: list[str] = []

        def inspect(record: _TerminalLivenessRecord) -> SimpleNamespace:
            inspected.append(record.session_id)
            return SimpleNamespace(state=OwnershipState.OWNERLESS)

        monkeypatch.setattr(liveness_mod, "inspect_foreground_ownership", inspect)
        monkeypatch.setattr(liveness_mod, "get_machine_id", lambda: local_machine)
        records = [
            replace(_external_record("tmux"), machine_id=record_machine),
            replace(_record("process", pane=None, window=None), machine_id=record_machine),
        ]
        monkeypatch.setattr(monitor, "_get_active_terminal_sessions", lambda: records)

        await monitor._check_sessions()

        assert (probed, inspected, storage.expire_calls) == ([], [], [])

    @pytest.mark.asyncio
    async def test_expiry_only_applies_to_the_probed_snapshot(
        self,
        monitor: SessionLivenessMonitor,
        storage: _Storage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _fake_tmux(monkeypatch, [])
        monkeypatch.setattr(
            monitor, "_get_active_terminal_sessions", lambda: [_external_record("a")]
        )

        await monitor._check_sessions()

        # A session rebound while the probe ran has moved on and must not match.
        assert storage.expire_snapshots == [("a", _LOCAL, _OBSERVED)]


class TestConditionalExpiry:
    @pytest.mark.asyncio
    async def test_success_dispatches_summary_and_unregisters(self) -> None:
        storage = _Storage(expire_result=SimpleNamespace(status="expired"))
        dispatch = MagicMock()
        stale_processor = _Processor()
        processor = _Processor()
        current = [stale_processor]
        monitor = SessionLivenessMonitor(
            session_storage=cast(Any, storage),
            dispatch_summaries_fn=dispatch,
            message_processor_resolver=lambda: cast(SessionMessageProcessor, current[0]),
        )
        current[0] = processor

        result = await monitor._expire_session("session", active_expiry=(_LOCAL, _OBSERVED))

        assert result is True
        assert storage.expire_calls == ["session"]
        dispatch.assert_called_once_with("session", False, None)
        assert processor.unregistered == ["session"]
        assert stale_processor.unregistered == []

    @pytest.mark.asyncio
    async def test_success_retires_session_hook_effects(self) -> None:
        storage = _Storage(expire_result=SimpleNamespace(status="expired"))
        monitor = SessionLivenessMonitor(session_storage=cast(Any, storage))

        assert callable(getattr(liveness_mod, "retire_session_hook_effects", None))
        with patch.object(liveness_mod, "retire_session_hook_effects") as retire:
            result = await monitor._expire_session("session", active_expiry=(_LOCAL, _OBSERVED))

        assert result is True
        retire.assert_called_once()
        assert retire.call_args.kwargs["session_id"] == "session"
        assert retire.call_args.args[0] is storage.db

    @pytest.mark.asyncio
    async def test_resolver_failure_is_best_effort_after_expiry(self) -> None:
        storage = _Storage(expire_result=SimpleNamespace(status="expired"))
        dispatch = MagicMock()

        def fail_resolver() -> SessionMessageProcessor | None:
            raise RuntimeError("processor runtime unavailable")

        monitor = SessionLivenessMonitor(
            session_storage=cast(Any, storage),
            dispatch_summaries_fn=dispatch,
            message_processor_resolver=fail_resolver,
        )

        result = await monitor._expire_session("session", active_expiry=(_LOCAL, _OBSERVED))

        assert result is True
        assert storage.expire_calls == ["session"]
        dispatch.assert_called_once_with("session", False, None)

    @pytest.mark.asyncio
    async def test_status_race_skips_summary_and_cleanup(self) -> None:
        storage = _Storage(expire_result=None)
        dispatch = MagicMock()
        processor = _Processor()
        monitor = SessionLivenessMonitor(
            session_storage=cast(Any, storage),
            dispatch_summaries_fn=dispatch,
            message_processor_resolver=lambda: cast(SessionMessageProcessor, processor),
        )

        result = await monitor._expire_session("session", active_expiry=(_LOCAL, _OBSERVED))

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

        result = await monitor._expire_session("session", active_expiry=(_LOCAL, _OBSERVED))

        assert result is True
        generate.assert_awaited_once_with("session")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("ownership", "agent_run_id", "released"),
        [
            ("gobby", None, True),
            ("gobby", "run-1", False),
            ("external", None, False),
        ],
    )
    async def test_expiry_cases_linked_terminal_row(
        self, ownership: str, agent_run_id: str | None, released: bool
    ) -> None:
        storage = _Storage(expire_result=SimpleNamespace(status="expired"))
        terminal = MagicMock()
        live = SimpleNamespace(id="term-1", ownership=ownership, agent_run_id=agent_run_id)
        terminal.get_live_for_session.return_value = live
        monitor = SessionLivenessMonitor(
            session_storage=cast(Any, storage),
            terminal_manager=terminal,
        )

        result = await monitor._expire_session("session", active_expiry=(_LOCAL, _OBSERVED))

        assert result is True
        terminal.get_live_for_session.assert_called_once_with("session")
        if released:
            # A bare gobby pane outlives the expired CLI and waits for the next session.
            terminal.release_session.assert_called_once_with("term-1", "session")
            terminal.mark_exited.assert_not_called()
        else:
            terminal.mark_exited.assert_called_once_with("term-1")
            terminal.release_session.assert_not_called()


class TestGetActiveTerminalSessions:
    def test_uses_central_eligible_statuses_and_parses_context(self, storage: _Storage) -> None:
        storage.db.fetchall.return_value = [
            {
                "id": "session",
                "source": "codex",
                "status": "awaiting_handoff",
                "machine_id": "21000000-0000-4000-8000-000000000003",
                "terminal_context": json.dumps(
                    {
                        "parent_pid": "42",
                        "parent_create_time": 10.0,
                        "tmux_pane": "%1",
                        "tty": "/dev/ttys001",
                    }
                ),
            }
        ]
        monitor = SessionLivenessMonitor(session_storage=cast(Any, storage))

        records = monitor._get_active_terminal_sessions()

        assert len(records) == 1
        assert records[0].status == "awaiting_handoff"
        assert records[0].parent_pid == 42
        query = storage.db.fetchall.call_args.args[0]
        assert "s.status = ANY(%s)" in query
        assert storage.db.fetchall.call_args.args[1] == (
            [
                "active",
                "paused",
                "interrupted",
                "awaiting_input",
                "awaiting_approval",
                "awaiting_handoff",
            ],
        )

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


def test_paused_native_id_only_context_is_liveness_candidate(storage: _Storage) -> None:
    observed = datetime(2026, 9, 27, tzinfo=UTC)
    terminal_id = "20000000-0000-4000-8000-000000000004"
    storage.db.fetchall.return_value = [
        {
            "id": "paused-seat",
            "source": "codex",
            "status": "paused",
            "machine_id": "21000000-0000-4000-8000-000000000003",
            "updated_at": observed,
            "terminal_context": {"gobby_terminal_id": terminal_id},
        }
    ]
    monitor = SessionLivenessMonitor(session_storage=cast(Any, storage))

    records = monitor._get_active_terminal_sessions()

    assert len(records) == 1
    assert records[0].terminal_context == {"gobby_terminal_id": terminal_id}
    assert records[0].updated_at == observed


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["confirmed", "remote", "active", "startup", "hub_outage"])
async def test_only_local_paused_exited_native_candidate_enters_guarded_expiry(
    storage: _Storage,
    case: str,
) -> None:
    observed = datetime(2026, 9, 27, tzinfo=UTC)
    local_id = "21000000-0000-4000-8000-000000000003"
    terminal_id = "20000000-0000-4000-8000-000000000004"
    storage.expire_result = object()
    storage.db.fetchall.return_value = [
        {
            "id": "paused-seat",
            "source": "codex",
            "status": "active" if case == "active" else "paused",
            "machine_id": "21000000-0000-4000-8000-000000000004" if case == "remote" else local_id,
            "updated_at": observed,
            "terminal_context": {"gobby_terminal_id": terminal_id},
        }
    ]
    if case == "hub_outage":
        storage.db.fetchall.side_effect = RuntimeError("hub unavailable")
    terminal_manager = MagicMock()
    terminal_manager.get_live_for_session.return_value = None
    monitor = SessionLivenessMonitor(
        session_storage=cast(Any, storage),
        terminal_manager=terminal_manager,
        startup_ready=lambda: case != "startup",
        live_host_epoch=lambda: "epoch-live",
    )
    with (
        patch.object(liveness_mod, "get_machine_id", return_value=local_id),
        patch.object(liveness_mod, "retire_session_hook_effects"),
    ):
        await monitor._check_sessions()

    expected = [("paused-seat", terminal_id, local_id, observed)] if case == "confirmed" else []
    assert storage.guarded_expire_calls == expected
    # The live host epoch reaches storage, which decides whether the exit was a drain.
    assert storage.live_host_epochs == (["epoch-live"] if case == "confirmed" else [])
    assert storage.expire_calls == []
