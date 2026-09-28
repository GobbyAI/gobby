from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions import liveness_monitor as liveness_mod
from gobby.sessions.liveness_monitor import (
    SessionLivenessMonitor,
    _TerminalLivenessRecord,
)
from gobby.sessions.processor import SessionMessageProcessor


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
        parent_pid=pid,
        tmux_pane=pane,
        tmux_window_id=window,
        status=status,
        machine_id="21000000-0000-4000-8000-000000000003",
        terminal_context=context,
    )


class _Storage:
    def __init__(self, expire_result: object | None = None) -> None:
        self.db = MagicMock()
        self.expire_result = expire_result
        self.expire_calls: list[str] = []
        self.guarded_expire_calls: list[tuple[str, str, str, datetime]] = []
        self.live_host_epochs: list[str | None] = []
        self.update = MagicMock()

    def expire_if_active(self, session_id: str) -> object | None:
        self.expire_calls.append(session_id)
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


class TestPaneOwnershipLifecycle:
    @pytest.mark.asyncio
    async def test_legacy_tmux_target_is_fenced_without_probe_or_expiry(
        self,
        monitor: SessionLivenessMonitor,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        owner = _record("owner")

        def forbidden(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("legacy tmux target was probed or mutated")

        monkeypatch.setattr(monitor, "_get_active_terminal_sessions", lambda: [owner])
        monkeypatch.setattr(monitor, "_expire_session", forbidden)
        await monitor._check_sessions()
        await monitor._check_sessions()

        assert monitor._legacy_tmux_fenced_ids == {"owner"}
        assert sum("liveness is fenced" in record.getMessage() for record in caplog.records) == 1


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

        result = await monitor._expire_session("session")

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
            result = await monitor._expire_session("session")

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

        result = await monitor._expire_session("session")

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

        result = await monitor._expire_session("session")

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
                        "tmux_socket_name": "gobby",
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
