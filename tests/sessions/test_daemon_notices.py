"""The daemon announces its own restarts: a hold before stopping, DAEMON BACK once healthy."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby import runner_lifecycle, runner_lifecycle_shutdown
from gobby.runner import GobbyRunner
from gobby.sessions import daemon_notices
from gobby.sessions.daemon_notices import (
    BACK_NOTICE,
    HOLD_NOTICE,
    restart_requested,
    send_daemon_notice,
    send_hold_notice_if_restarting,
)
from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent
from gobby.storage.hub.operation_deadline import (
    DatabaseOperationDeadline,
    current_database_operation_deadline,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager, system_session_id
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


class RecordingWakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        self.calls.append(session_id)
        return {"session_id": session_id, "delivered": True, "method": "fake"}


def _runner(**fields: Any) -> GobbyRunner:
    return cast(GobbyRunner, SimpleNamespace(**fields))


@pytest.fixture
def marker_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("gobby.shutdown_intent.get_gobby_home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_notice_is_a_system_global_that_wakes_active_seats(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    recipients = [
        session_manager.register(
            external_id=name,
            machine_id=None,
            source="codex",
            project_id=sample_project["id"],
            title=name,
        ).id
        for name in ("active-seat", "second-active-seat")
    ]
    dispatcher = RecordingWakeDispatcher()
    runner = _runner(database=temp_db, session_manager=session_manager, wake_dispatcher=dispatcher)

    await send_daemon_notice(runner, BACK_NOTICE)

    assert dispatcher.calls == recipients
    rows = temp_db.fetchall(
        "SELECT from_session, to_session, content, message_type, metadata_json "
        "FROM inter_session_messages ORDER BY to_session"
    )
    assert sorted(row["to_session"] for row in rows) == sorted(recipients)
    for row in rows:
        assert row["from_session"] == system_session_id()
        assert row["content"] == BACK_NOTICE
        assert row["message_type"] == "daemon_notice"
        metadata = row["metadata_json"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        assert metadata["wake_requested"] is True


@pytest.mark.asyncio
async def test_restart_notice_wakes_active_seats_and_seats_holding_a_task_only(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    active = session_manager.register(
        external_id="active-seat",
        machine_id=None,
        source="codex",
        project_id=project_id,
        title="active-seat",
    ).id
    parked: list[str] = []
    for name, status in (("idle-seat", "idle"), ("paused-seat", "paused"), ("waiting-seat", "waiting")):
        seat = session_manager.register(
            external_id=name,
            machine_id=None,
            source="codex",
            project_id=project_id,
            title=name,
        ).id
        session_manager.update_status(seat, status)
        parked.append(seat)
    claimed = session_manager.register(
        external_id="claimed-idle-seat",
        machine_id=None,
        source="codex",
        project_id=project_id,
        title="claimed-idle-seat",
    ).id
    session_manager.update_status(claimed, "idle")
    LocalTaskManager(temp_db).create_task(
        project_id=project_id,
        title="Held through the restart",
        claimed_by_session_id=claimed,
        category="code",
        validation_criteria="The restart notice still wakes the claimed seat.",
        implementation_domain="backend",
    )
    dispatcher = RecordingWakeDispatcher()
    runner = _runner(database=temp_db, session_manager=session_manager, wake_dispatcher=dispatcher)

    await send_daemon_notice(runner, HOLD_NOTICE)

    assert sorted(dispatcher.calls) == sorted([active, claimed])
    rows = temp_db.fetchall(
        "SELECT to_session, content, metadata_json FROM inter_session_messages"
    )
    assert sorted(row["to_session"] for row in rows) == sorted([active, claimed, *parked])
    for row in rows:
        assert row["content"] == HOLD_NOTICE
        metadata = row["metadata_json"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        assert metadata["wake_requested"] is (row["to_session"] in {active, claimed})


@pytest.mark.asyncio
async def test_notice_failure_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    database = MagicMock()
    database.transaction.side_effect = RuntimeError("hub unavailable")
    runner = _runner(database=database, session_manager=MagicMock(), wake_dispatcher=None)

    await send_daemon_notice(runner, HOLD_NOTICE)

    assert "Daemon notice failed" in caplog.text


class StalledHub:
    """A hub whose transaction blocks past any notice bound, as a stalled pool wait does."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.thread: threading.Thread | None = None
        self.deadline: DatabaseOperationDeadline | None = None

    def transaction(self) -> NoReturn:
        self.thread = threading.current_thread()
        self.deadline = current_database_operation_deadline()
        self.entered.set()
        self.release.wait(timeout=5)
        raise RuntimeError("stalled transaction released")


@pytest.mark.asyncio
async def test_stalled_persistence_never_stalls_the_event_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = StalledHub()
    runner = _runner(database=hub, session_manager=MagicMock(), wake_dispatcher=None)
    started = time.monotonic()
    notice = asyncio.create_task(send_daemon_notice(runner, HOLD_NOTICE, timeout_seconds=0.1))
    try:
        assert await asyncio.to_thread(hub.entered.wait, 5)
        # The loop runs this test while the transaction is still stalled.
        assert not notice.done()
        await notice
        elapsed = time.monotonic() - started
        assert not hub.release.is_set()
    finally:
        hub.release.set()

    assert elapsed < 1.0
    assert hub.thread is not threading.main_thread()
    assert hub.deadline is not None
    assert hub.deadline.expires_at <= started + 0.1 + 0.05
    assert "Daemon notice exceeded" in caplog.text


class SlowWakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        self.calls.append(session_id)
        await asyncio.Event().wait()
        return {"session_id": session_id, "delivered": True, "method": "fake"}


@pytest.mark.asyncio
async def test_slow_wake_is_bounded_after_the_notice_commits(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    recipient = session_manager.register(
        external_id="slow-seat",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
        title="slow-seat",
    ).id
    dispatcher = SlowWakeDispatcher()
    runner = _runner(database=temp_db, session_manager=session_manager, wake_dispatcher=dispatcher)

    started = time.monotonic()
    await send_daemon_notice(runner, HOLD_NOTICE, timeout_seconds=0.2)

    assert time.monotonic() - started < 1.0
    assert dispatcher.calls == [recipient]
    rows = temp_db.fetchall("SELECT to_session, content FROM inter_session_messages")
    assert [(row["to_session"], row["content"]) for row in rows] == [(recipient, HOLD_NOTICE)]
    assert "Daemon notice exceeded" in caplog.text


def test_hold_text_names_the_tools_that_stay_allowed() -> None:
    for tool in (
        "gobby-sessions:get_handoff",
        "gobby-agents:wait_for_coordination",
        "gobby-agents:send_message",
    ):
        assert tool in HOLD_NOTICE
    assert "required" in HOLD_NOTICE
    assert BACK_NOTICE.startswith("DAEMON BACK")


@pytest.mark.parametrize(
    ("runner_intent", "marker_intent", "expected"),
    [
        # `gobby restart` (backend) sets the runner's restart intent.
        (ShutdownIntent.RESTART, None, True),
        # `gobby cutover` and `restart --full` stop through /shutdown, whose runner
        # intent is STOP; only the marker carries the restart.
        (ShutdownIntent.STOP, ShutdownIntent.RESTART, True),
        (ShutdownIntent.STOP, ShutdownIntent.STOP, False),
        (ShutdownIntent.STOP, None, False),
    ],
)
def test_restart_detection(
    marker_home: Path,
    runner_intent: ShutdownIntent,
    marker_intent: ShutdownIntent | None,
    expected: bool,
) -> None:
    if marker_intent is not None:
        write_shutdown_intent("http_shutdown", marker_intent, home=marker_home)

    assert restart_requested(_runner(_shutdown_intent=runner_intent)) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "sent"), [(ShutdownIntent.RESTART, [HOLD_NOTICE]), (ShutdownIntent.STOP, [])]
)
async def test_hold_is_sent_only_for_a_restart(
    marker_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    intent: ShutdownIntent,
    sent: list[str],
) -> None:
    send = AsyncMock()
    monkeypatch.setattr(daemon_notices, "send_daemon_notice", send)
    runner = _runner(_shutdown_intent=intent)

    await send_hold_notice_if_restarting(runner)

    assert [call.args[1] for call in send.await_args_list] == sent


@pytest.mark.asyncio
async def test_shutdown_sends_the_hold_before_the_terminal_host_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def hold(_runner: GobbyRunner) -> None:
        order.append("hold")

    class HostStopped(Exception):
        pass

    def begin_shutdown() -> None:
        order.append("host")
        raise HostStopped

    monkeypatch.setattr(runner_lifecycle_shutdown, "send_hold_notice_if_restarting", hold)
    runner = _runner(terminal_host_manager=SimpleNamespace(begin_shutdown=begin_shutdown))

    with pytest.raises(HostStopped):
        await runner_lifecycle_shutdown.shutdown_daemon_services(
            runner,
            MagicMock(),
            MagicMock(),
            1,
            await_critical_stop_hook_grace_window=AsyncMock(),
            shutdown_websocket_server=AsyncMock(),
            reap_remaining_child_processes=AsyncMock(),
            shutdown_telemetry=MagicMock(),
            cleanup_pid_file=MagicMock(),
        )

    assert order == ["hold", "host"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_exit", ["restart", "cutover", "unclean"])
async def test_every_healthy_start_sends_one_daemon_back(
    marker_home: Path, monkeypatch: pytest.MonkeyPatch, prior_exit: str
) -> None:
    if prior_exit != "unclean":
        write_shutdown_intent(f"cli_{prior_exit}", ShutdownIntent.RESTART, home=marker_home)
    sent: list[tuple[object, str]] = []

    async def record_notice(target: object, content: str) -> None:
        sent.append((target, content))

    async def init_ok(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner_lifecycle, "send_daemon_notice", record_notice)
    monkeypatch.setattr(runner_lifecycle, "init_subsystems", init_ok)
    runner = _runner()

    await runner_lifecycle._init_subsystems(runner, None)

    assert sent == [(runner, BACK_NOTICE)]


@pytest.mark.asyncio
async def test_failed_start_sends_no_daemon_back(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    async def record_notice(_target: object, content: str) -> None:
        sent.append(content)

    async def init_fails(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(runner_lifecycle, "send_daemon_notice", record_notice)
    monkeypatch.setattr(runner_lifecycle, "init_subsystems", init_fails)

    with pytest.raises(RuntimeError, match="boom"):
        await runner_lifecycle._init_subsystems(_runner(), None)

    assert sent == []
