"""Daemon supervision of gterm (plan 3.1 host manager)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import stat
import threading
import tomllib
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.config.terminals import TerminalConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import AttachLocator, Terminal, TerminalManager, native_locator_key
from gobby.terminals import host_event_reader, host_events
from gobby.terminals.frame_client import FrameClient
from gobby.terminals.host_client import HostManagerStopped
from gobby.terminals.host_events import (
    HostInventorySnapshot,
    InputActivityEvent,
    TerminalExitedEvent,
)
from gobby.terminals.host_protocol import HostListRow
from gobby.terminals.host_reconcile import ReconcileError, reconcile_host_inventory
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.utils.machine_id import require_machine_id
from tests._timing import wait_for_condition
from tests.terminals.host_fakes import (
    FakeControlClient,
    FakeHostProcess,
    FakeListRow,
    FakeRunManager,
)

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _loaded(manager: TerminalManager, terminal_id: str) -> Terminal:
    row = manager.get(terminal_id)
    assert row is not None
    return row


def _pending(
    manager: TerminalManager,
    project_id: str,
    *,
    spawn_key: str | None = None,
    agent_run_id: str | None = None,
) -> Any:
    tid = str(uuid.uuid4())
    return manager.create_pending(
        terminal_id=tid,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=spawn_key or tid,
        machine_id=require_machine_id(),
        agent_run_id=agent_run_id,
    )


def _live(
    manager: TerminalManager,
    project_id: str,
    epoch: str,
    *,
    host_terminal_id: str = "ht-1",
    agent_run_id: str | None = None,
) -> Any:
    row = _pending(manager, project_id, agent_run_id=agent_run_id)
    promoted = manager.promote_to_live(
        row.id,
        locator={"host_terminal_id": host_terminal_id},
        locator_key=native_locator_key(epoch, host_terminal_id),
        host_epoch=epoch,
    )
    assert promoted is not None
    return promoted


def _host(
    tmp_path: Path,
    terminal_manager: TerminalManager,
    client: FakeControlClient,
    *,
    run_manager: FakeRunManager | None = None,
    process: FakeHostProcess | None = None,
    pid_ok: bool = True,
    spawn_in_doubt_seconds: float = 150.0,
) -> Any:
    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.terminals.host_manager import TerminalHostManager

    proc = process or FakeHostProcess(pid=client.host_pid)

    async def connect() -> FakeControlClient:
        return client

    def spawn() -> FakeHostProcess:
        return proc

    return TerminalHostManager(
        config=TerminalHostConfig(
            enabled=True,
            socket_dir=str(tmp_path),
            shutdown_grace_seconds=0.2,
            health_interval_seconds=3600.0,
        ),
        terminal_config=TerminalConfig(spawn_in_doubt_seconds=spawn_in_doubt_seconds),
        terminal_manager=terminal_manager,
        run_manager=run_manager or FakeRunManager(),
        connector=connect,
        spawner=spawn,
        pid_identity=lambda _pid: pid_ok,
    )


def _capture_terminal_lifecycle(
    terminals: TerminalManager,
) -> tuple[list[dict[str, Any]], asyncio.Event, TerminalLeaseRegistry]:
    from gobby.runner_broadcasting import setup_terminal_lifecycle_broadcasting

    loop = asyncio.get_running_loop()
    events: list[dict[str, Any]] = []
    emitted = asyncio.Event()
    registry = TerminalLeaseRegistry(daemon_epoch="test-daemon")

    async def publish(event: dict[str, Any]) -> None:
        events.append(event)
        emitted.set()

    setup_terminal_lifecycle_broadcasting(
        terminals,
        registry,
        publish,
        loop_getter=lambda: loop,
    )
    return events, emitted, registry


async def _stop_terminal_lifecycle_capture(
    terminals: TerminalManager,
    registry: TerminalLeaseRegistry,
) -> None:
    terminals.set_transition_observer(None)
    await registry.shutdown_lifecycle_publication()


def test_health_state_observes_recorded_host_pid(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    client = FakeControlClient(host_pid=9100)
    host = _host(tmp_path, TerminalManager(temp_db), client, pid_ok=False)
    host.running = True
    host.host_pid = client.host_pid

    assert host.health_state()["running"] is False
    assert host.running is True


class _ControlledSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        self.started.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_gap_settles_indeterminate_from_list(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    class InventoryClient(FakeControlClient):
        async def list_inventory(self) -> HostInventorySnapshot:
            rows = tuple(cast(HostListRow, row) for row in self.terminals)
            return HostInventorySnapshot(rows, self.host_epoch, 17)

    class GapStream:
        epoch = "epoch-gap"
        seq = 4
        gap = True

        def __aiter__(self) -> GapStream:
            return self

        async def __anext__(self) -> Any:
            await asyncio.Future()

        async def aclose(self) -> None:
            return None

    terminals = TerminalManager(temp_db)
    committed = _pending(terminals, sample_project["id"])
    prepared = _pending(terminals, sample_project["id"])
    absent = _pending(terminals, sample_project["id"])
    client = InventoryClient(
        host_epoch="epoch-gap",
        terminals=[
            FakeListRow(
                terminal_id=committed.id,
                spawn_key=committed.spawn_key or committed.id,
                commit_state="committed",
                host_terminal_id="ht-committed",
            ),
            FakeListRow(
                terminal_id=prepared.id,
                spawn_key=prepared.spawn_key or prepared.id,
                commit_state="prepared",
                host_terminal_id="ht-prepared",
            ),
        ],
    )
    client.authed = True
    host = _host(tmp_path, terminals, client)
    host._client = client

    await host_event_reader.recover_event_gap(host, cast(host_events.HostEventStream, GapStream()))

    assert _loaded(terminals, committed.id).state == "live"
    assert _loaded(terminals, prepared.id).state == "exited"
    assert _loaded(terminals, absent.id).state == "exited"
    assert client.kill_calls == ["ht-prepared"]
    assert (host.last_event_epoch, host.last_event_seq) == ("epoch-gap", 17)


@pytest.mark.asyncio
async def test_reconcile_catches_only_typed_errors(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    terminals = TerminalManager(temp_db)
    client = FakeControlClient(host_epoch="epoch-errors")
    host = _host(tmp_path, terminals, client)
    host._client = client

    with patch(
        "gobby.terminals.host_manager.reconcile_host_inventory",
        new=AsyncMock(side_effect=ReconcileError("expected inventory race")),
    ):
        await host.reconcile(host_rows=[])
    assert host.last_error == "expected inventory race"

    with patch(
        "gobby.terminals.host_manager.reconcile_host_inventory",
        new=AsyncMock(side_effect=RuntimeError("database invariant")),
    ):
        with pytest.raises(RuntimeError, match="database invariant"):
            await host.reconcile(host_rows=[])

    with patch.object(
        terminals,
        "list_reconcilable_by_machine",
        side_effect=RuntimeError("database read failed"),
    ):
        with pytest.raises(RuntimeError, match="database read failed"):
            await reconcile_host_inventory(
                terminal_manager=terminals,
                machine_id=LOCAL_MACHINE_ID,
                host_epoch="epoch-errors",
                host_rows=[],
                spawn_in_doubt_seconds=30.0,
                run_manager=None,
                kill=AsyncMock(),
            )


@pytest.mark.asyncio
async def test_reap_runs_off_loop(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    terminals = TerminalManager(temp_db)
    host = _host(tmp_path, terminals, FakeControlClient())
    started = threading.Event()
    release = threading.Event()

    def blocking_reap(process: dict[str, object], *, grace_seconds: float) -> None:
        assert process["pgid"] == 42
        assert grace_seconds == host.config.shutdown_grace_seconds
        started.set()
        release.wait(timeout=1.0)

    loop_tick = asyncio.Event()
    with patch("gobby.terminals.host_manager.reap_recorded_process", blocking_reap):
        task = asyncio.create_task(host.reap_recorded_process({"pgid": 42, "start_time": 1}))
        asyncio.get_running_loop().call_soon(loop_tick.set)
        await loop_tick.wait()
        assert task.done() is False
        assert await asyncio.to_thread(started.wait, 1.0)
        release.set()
        await task


@pytest.mark.asyncio
async def test_gap_recovery_converges_under_ring_churn(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    epoch = "epoch-churn"
    terminals = TerminalManager(temp_db)
    first = _live(terminals, sample_project["id"], epoch, host_terminal_id="ht-first")
    second = _live(terminals, sample_project["id"], epoch, host_terminal_id="ht-second")
    host_rows = [
        FakeListRow(
            terminal_id=first.id,
            spawn_key=first.spawn_key or first.id,
            host_terminal_id="ht-first",
        ),
        FakeListRow(
            terminal_id=second.id,
            spawn_key=second.spawn_key or second.id,
            host_terminal_id="ht-second",
        ),
    ]

    class InventoryClient(FakeControlClient):
        async def list_inventory(self) -> HostInventorySnapshot:
            rows = tuple(cast(HostListRow, row) for row in self.terminals)
            return HostInventorySnapshot(rows, self.host_epoch, 10_000)

    class ChurningStream:
        epoch = "epoch-churn"
        seq = 9_000
        gap = True

        def __init__(self) -> None:
            self.events = [
                TerminalExitedEvent(first.id, "ht-first", 0, epoch, 9_999),
                TerminalExitedEvent(first.id, "ht-first", 0, epoch, 10_000),
                TerminalExitedEvent(first.id, "ht-first", 0, epoch, 10_001),
                TerminalExitedEvent(second.id, "ht-second", 7, epoch, 10_002),
            ]
            self.closed = False

        def __aiter__(self) -> ChurningStream:
            return self

        async def __anext__(self) -> TerminalExitedEvent:
            if self.events:
                return self.events.pop(0)
            raise HostManagerStopped()

        async def aclose(self) -> None:
            self.closed = True

    client = InventoryClient(host_epoch=epoch, terminals=host_rows)
    client.authed = True
    stream = ChurningStream()
    subscriptions = 0

    async def connect_events(since: int | None) -> Any:
        nonlocal subscriptions
        assert since is None
        subscriptions += 1
        return stream

    host = _host(tmp_path, terminals, client)
    host._client = client
    host.host_epoch = epoch
    host._event_connector = connect_events
    settled: list[tuple[str, str]] = []
    original_settle = terminals.settle_exit

    def record_settle(terminal_id: str, host_terminal_id: str) -> Terminal | None:
        settled.append((terminal_id, host_terminal_id))
        return original_settle(terminal_id, host_terminal_id)

    with patch.object(terminals, "settle_exit", side_effect=record_settle):
        await host_event_reader.event_reader_loop(host)

    assert subscriptions == 1
    assert settled == [(first.id, "ht-first"), (second.id, "ht-second")]
    assert (host.last_event_epoch, host.last_event_seq) == (epoch, 10_002)
    assert stream.closed is True


@pytest.mark.asyncio
async def test_gap_buffer_overflow_repeats_cut(
    tmp_path: Path,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = "epoch-overflow"

    class OverflowStream:
        epoch = "epoch-overflow"
        seq = 0
        gap = True

        def __init__(self) -> None:
            self.first_batch = asyncio.Event()
            self.allow_second = asyncio.Event()
            self.second_delivered = asyncio.Event()
            self.events = [1, 2, 3, 5]
            self.terminal_ids = {seq: str(uuid.uuid4()) for seq in self.events}

        def __aiter__(self) -> OverflowStream:
            return self

        async def __anext__(self) -> TerminalExitedEvent:
            if self.events[0] == 5:
                await self.allow_second.wait()
            seq = self.events.pop(0)
            if seq == 3:
                self.first_batch.set()
            if seq == 5:
                self.second_delivered.set()
            return TerminalExitedEvent(self.terminal_ids[seq], f"host-{seq}", seq, epoch, seq)

        async def aclose(self) -> None:
            return None

    stream = OverflowStream()

    class InventoryClient(FakeControlClient):
        list_calls = 0

        async def list_inventory(self) -> HostInventorySnapshot:
            self.list_calls += 1
            if self.list_calls == 1:
                await stream.first_batch.wait()
                return HostInventorySnapshot((), epoch, 3)
            stream.allow_second.set()
            await stream.second_delivered.wait()
            return HostInventorySnapshot((), epoch, 4)

    client = InventoryClient(host_epoch=epoch)
    client.authed = True
    host = _host(tmp_path, TerminalManager(temp_db), client)
    host._client = client
    monkeypatch.setattr(host_events, "GAP_BUFFER_ENTRIES", 2)

    await host_event_reader.recover_event_gap(host, cast(host_events.HostEventStream, stream))

    assert client.list_calls == 2
    assert (host.last_event_epoch, host.last_event_seq) == (epoch, 5)


@pytest.mark.asyncio
async def test_input_activity_reaches_sink_not_settle_exit(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    epoch = "epoch-input"
    terminals = TerminalManager(temp_db)
    pending = _pending(terminals, sample_project["id"])
    terminals.record_process(
        pending.id,
        {"host_terminal_id": "ht-typed"},
        attempt_generation=pending.attempt_generation,
        attempt_started_at=pending.attempt_started_at,
    )
    row = terminals.promote_to_live(
        pending.id,
        locator={"host_terminal_id": "ht-typed"},
        locator_key=native_locator_key(epoch, "ht-typed"),
        host_epoch=epoch,
    )
    assert row is not None
    client = FakeControlClient(host_epoch=epoch)
    client.authed = True
    host = _host(tmp_path, terminals, client)
    host._client = client
    host.host_epoch = epoch
    host.last_event_epoch = epoch
    host.last_event_seq = 4
    seen: list[InputActivityEvent] = []
    host.set_input_activity_sink(seen.append)
    typed = InputActivityEvent(row.id, "ht-typed", "att-1", "input", 1, "ctrl_c", epoch, 5)

    def failing_sink(_event: InputActivityEvent) -> None:
        raise RuntimeError("sink down")

    with patch.object(terminals, "settle_exit", wraps=terminals.settle_exit) as settle:
        await host_event_reader.apply_host_event(host, typed)
        assert seen == [typed]
        settle.assert_not_called()
        assert host.last_event_seq == 5
        # A replayed sequence is dropped like any other duplicate.
        await host_event_reader.apply_host_event(host, typed)
        assert seen == [typed]
        # A failing sink is logged, never propagated, and never stalls the cursor.
        host.set_input_activity_sink(failing_sink)
        await host_event_reader.apply_host_event(
            host, InputActivityEvent(row.id, "ht-typed", "att-1", "paste", 3, None, epoch, 6)
        )
        assert host.last_event_seq == 6
        await host_event_reader.apply_host_event(
            host, TerminalExitedEvent(row.id, "ht-typed", 0, epoch, 7)
        )
        settle.assert_called_once_with(row.id, "ht-typed")
    assert host.last_event_seq == 7
    assert _loaded(terminals, row.id).state == "exited"


@pytest.mark.asyncio
async def test_host_terminal_exited_event_broadcasts_exited(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    epoch = "epoch-exit-event"
    terminals = TerminalManager(temp_db)
    pending = _pending(terminals, sample_project["id"])
    terminals.record_process(
        pending.id,
        {"host_terminal_id": "ht-exited"},
        attempt_generation=pending.attempt_generation,
        attempt_started_at=pending.attempt_started_at,
    )
    row = terminals.promote_to_live(
        pending.id,
        locator={"host_terminal_id": "ht-exited"},
        locator_key=native_locator_key(epoch, "ht-exited"),
        host_epoch=epoch,
    )
    assert row is not None
    client = FakeControlClient(host_epoch=epoch)
    host = _host(tmp_path, terminals, client)
    host.last_event_epoch = epoch
    events, emitted, registry = _capture_terminal_lifecycle(terminals)

    try:
        await host_event_reader.apply_host_event(
            host,
            TerminalExitedEvent(row.id, "ht-exited", 0, epoch, 1),
        )
        await asyncio.wait_for(emitted.wait(), timeout=1.0)
        assert terminals.mark_exited(row.id) is None
    finally:
        await _stop_terminal_lifecycle_capture(terminals, registry)

    assert _loaded(terminals, row.id).state == "exited"
    assert len(events) == 1
    assert events[0]["daemon_epoch"] == "test-daemon"
    assert events[0]["seq"] == 1
    assert events[0]["type"] == "terminal_event"
    assert events[0]["event"] == "exited"
    assert events[0]["terminal_id"] == row.id


@pytest.mark.asyncio
async def test_reconcile_orphan_marking_broadcasts_orphaned(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    terminals = TerminalManager(temp_db)
    old_epoch = "epoch-before-restart"
    row = _live(terminals, sample_project["id"], old_epoch)
    events, emitted, registry = _capture_terminal_lifecycle(terminals)

    try:
        await reconcile_host_inventory(
            terminal_manager=terminals,
            machine_id=LOCAL_MACHINE_ID,
            host_epoch="epoch-after-restart",
            host_rows=[],
            spawn_in_doubt_seconds=0.0,
            run_manager=None,
            kill=AsyncMock(),
        )
        await asyncio.wait_for(emitted.wait(), timeout=1.0)
    finally:
        await _stop_terminal_lifecycle_capture(terminals, registry)

    assert _loaded(terminals, row.id).state == "orphaned"
    assert len(events) == 1
    assert events[0]["type"] == "terminal_event"
    assert events[0]["event"] == "orphaned"
    assert events[0]["terminal_id"] == row.id


@pytest.mark.asyncio
async def test_reconcile_exits_dead_epoch_orphan_with_absent_process_group(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    terminals = TerminalManager(temp_db)
    pending = _pending(terminals, sample_project["id"])
    terminals.record_process(
        pending.id,
        {"host_terminal_id": "ht-orphaned", "pgid": 987654321},
        attempt_generation=pending.attempt_generation,
        attempt_started_at=pending.attempt_started_at,
    )
    row = terminals.promote_to_live(
        pending.id,
        locator={"host_terminal_id": "ht-orphaned"},
        locator_key=native_locator_key("dead-epoch", "ht-orphaned"),
        host_epoch="dead-epoch",
    )
    assert row is not None
    orphaned = terminals.mark_orphaned(row.id)
    assert orphaned is not None
    events, emitted, registry = _capture_terminal_lifecycle(terminals)

    try:
        with (
            patch("gobby.terminals.host_reap.os.killpg", side_effect=ProcessLookupError) as killpg,
            patch("gobby.terminals.host_reap.os.kill", side_effect=ProcessLookupError) as kill,
        ):
            await reconcile_host_inventory(
                terminal_manager=terminals,
                machine_id=LOCAL_MACHINE_ID,
                host_epoch="current-epoch",
                host_rows=[],
                spawn_in_doubt_seconds=0.0,
                run_manager=None,
                kill=AsyncMock(),
            )
        await asyncio.wait_for(emitted.wait(), timeout=1.0)
    finally:
        await _stop_terminal_lifecycle_capture(terminals, registry)

    killpg.assert_called_once_with(987654321, 0)
    kill.assert_called_once_with(987654321, 0)
    assert _loaded(terminals, row.id).state == "exited"
    assert len(events) == 1
    assert events[0]["type"] == "terminal_event"
    assert events[0]["event"] == "exited"
    assert events[0]["terminal_id"] == row.id


@pytest.mark.asyncio
async def test_event_reader_joins_singleflight_and_stops_cleanly(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    terminals = TerminalManager(temp_db)
    client = FakeControlClient(host_epoch="epoch-reader")

    async def crash_stream(_since: int | None) -> Any:
        raise ConnectionError("host crashed")

    host = _host(tmp_path, terminals, client, pid_ok=False)
    host.host_epoch = client.host_epoch
    host.host_pid = client.host_pid
    host._event_connector = crash_stream
    restart = AsyncMock(side_effect=HostManagerStopped())
    with patch.object(host, "ensure_restart", new=restart):
        host_event_reader.arm_events(host)
        event_task = host._event_task
        host_event_reader.arm_events(host)
        assert host._event_task is event_task
        assert event_task is not None
        await event_task
    restart.assert_awaited_once()

    spawn = MagicMock()

    async def stopped_stream(_since: int | None) -> Any:
        raise HostManagerStopped()

    stopped = _host(tmp_path, terminals, client, process=FakeHostProcess(pid=client.host_pid))
    stopped._spawner = spawn
    stopped._event_connector = stopped_stream
    stopped.last_event_epoch = "epoch-before-stop"
    stopped.last_event_seq = 23
    host_event_reader.arm_events(stopped)
    assert stopped._event_task is not None
    await stopped._event_task
    assert (stopped.last_event_epoch, stopped.last_event_seq) == ("epoch-before-stop", 23)
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_fake_host_has_no_attach_verb() -> None:
    client = FakeControlClient()
    await client.hello(client.protocol_version, client.token)

    response = await client.dispatch("attach")

    assert response == {"ok": False, "error": "unknown_verb"}
    assert not hasattr(client, "attach")


@pytest.mark.asyncio
async def test_host_shutdown_escalates(
    tmp_path: Path,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminals = TerminalManager(temp_db)
    cases = [
        ([True], [], "host_shutdown"),
        ([False, True], [signal.SIGTERM], "SIGTERM"),
        ([False, False, True], [signal.SIGTERM, signal.SIGKILL], "SIGKILL"),
    ]

    for index, (exit_results, expected_signals, rung) in enumerate(cases):
        pid = 7_400 + index
        client = FakeControlClient(host_pid=pid, authed=True)
        host = _host(tmp_path, terminals, client)
        host._client = client
        host.host_pid = pid
        wait_for_exit = AsyncMock(side_effect=exit_results)
        monkeypatch.setattr(host, "_await_host_exit", wait_for_exit)

        with patch("gobby.terminals.host_manager.os.kill") as kill:
            await host._host_shutdown()

        assert client.shutdown_calls == [200]
        assert wait_for_exit.await_args_list == [call(pid)] * len(exit_results)
        assert [args.args[1] for args in kill.call_args_list] == expected_signals
        assert host.last_error == f"gterm host {pid} exited after {rung}"
        assert host._host_exit_deadline_seconds() == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_crash_orphans_and_interrupts(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    runs = FakeRunManager()
    old_epoch = str(uuid.uuid4())
    row = _live(terminals, sample_project["id"], old_epoch)
    client = FakeControlClient(host_epoch=str(uuid.uuid4()), host_pid=9100)
    write_pidfile(tmp_path, client.host_pid)
    host = _host(tmp_path, terminals, client, run_manager=runs)
    await host.start()
    await host.handle_host_death()
    updated = terminals.get(row.id)
    assert updated is not None
    assert updated.state == "exited"
    host._interrupt("run-orphaned")
    assert "run-orphaned" in runs.interrupted


@pytest.mark.asyncio
async def test_degraded_startup_without_host(tmp_path: Path, temp_db: HubDatabase) -> None:
    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.terminals.host_manager import TerminalHostManager

    async def boom() -> FakeControlClient:
        raise ConnectionRefusedError("no socket")

    def missing() -> FakeHostProcess:
        raise FileNotFoundError("gterm")

    host = TerminalHostManager(
        config=TerminalHostConfig(enabled=True, socket_dir=str(tmp_path)),
        terminal_config=TerminalConfig(),
        terminal_manager=TerminalManager(temp_db),
        connector=boom,
        spawner=missing,
    )
    await host.start()
    assert host.running is False
    assert host.native_available is False
    assert host.last_error
    state = host.health_state()
    assert state["enabled"] is True
    assert state["running"] is False
    assert state["last_error"]
    # The degraded start arms the retry loop (#22425); stop cancels it.
    await host.stop()


@pytest.mark.asyncio
async def test_startup_settles_after_start_even_when_degraded(
    tmp_path: Path, temp_db: HubDatabase
) -> None:
    """Attach paths wait on startup settling, so a degraded start settles too (#22002)."""
    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.terminals.host_manager import TerminalHostManager

    async def boom() -> FakeControlClient:
        raise ConnectionRefusedError("no socket")

    def missing() -> FakeHostProcess:
        raise FileNotFoundError("gterm")

    host = TerminalHostManager(
        config=TerminalHostConfig(enabled=True, socket_dir=str(tmp_path)),
        terminal_config=TerminalConfig(),
        terminal_manager=TerminalManager(temp_db),
        connector=boom,
        spawner=missing,
    )
    assert await host.wait_startup_settled(0.01) is False
    await host.start()
    assert host.native_available is False
    assert await host.wait_startup_settled(0.01) is True
    await host.stop()


@pytest.mark.asyncio
async def test_restart_adopts_host_preserving_epoch_pid_and_row(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    row = _live(terminals, sample_project["id"], epoch)
    client = FakeControlClient(
        host_epoch=epoch,
        host_pid=7777,
        terminals=[
            FakeListRow(
                terminal_id=row.id,
                spawn_key=row.spawn_key or row.id,
                commit_state="committed",
                host_terminal_id="ht-1",
            )
        ],
    )
    write_pidfile(tmp_path, 7777)
    host = _host(tmp_path, terminals, client)
    await host.start()
    assert host.adopted is True
    assert host.spawned_this_construction is False
    assert host.host_epoch == epoch
    assert host.host_pid == 7777
    loaded = _loaded(terminals, row.id)
    assert loaded.state == "live"
    assert loaded.host_epoch == epoch


@pytest.mark.asyncio
async def test_start_adopts_surviving_host_with_live_terminals(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """A host that outlived the previous daemon is adopted with its terminals intact."""
    from gobby.terminals.host_protocol import CONTROL_TOKEN_FILE_NAME, write_pidfile

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    first = _live(terminals, sample_project["id"], epoch, host_terminal_id="ht-1")
    second = _live(terminals, sample_project["id"], epoch, host_terminal_id="ht-2")
    client = FakeControlClient(
        host_epoch=epoch,
        host_pid=7001,
        terminals=[
            FakeListRow(
                terminal_id=first.id,
                spawn_key=first.spawn_key or first.id,
                host_terminal_id="ht-1",
            ),
            FakeListRow(
                terminal_id=second.id,
                spawn_key=second.spawn_key or second.id,
                host_terminal_id="ht-2",
            ),
        ],
    )
    write_pidfile(tmp_path, 7001)
    host = _host(tmp_path, terminals, client)
    token_before = host.ensure_control_token()
    await host.start()

    assert host.adopted is True
    assert host.spawned_this_construction is False
    assert host.running is True
    assert host.host_pid == 7001
    assert host.preserved_host_pid() == 7001
    assert client.shutdown_calls == []
    assert client.kill_calls == []
    assert (tmp_path / CONTROL_TOKEN_FILE_NAME).read_text() == token_before
    assert _loaded(terminals, first.id).state == "live"
    assert _loaded(terminals, second.id).state == "live"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["protocol", "token", "pid_identity"])
async def test_protocol_mismatch_keeps_existing_host_alive(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    mismatch: str,
) -> None:
    """An unadoptable host degrades the daemon; it is never drained or replaced."""
    from gobby.terminals.host_protocol import CONTROL_TOKEN_FILE_NAME, write_pidfile

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    row = _live(terminals, sample_project["id"], epoch)
    client = FakeControlClient(
        host_epoch=epoch,
        host_pid=7002,
        terminals=[FakeListRow(terminal_id=row.id, spawn_key=row.spawn_key or row.id)],
    )
    pid_ok = True
    if mismatch == "protocol":
        client.protocol_version = 2
    elif mismatch == "token":
        client.token = "rotated-by-someone-else"
    else:
        pid_ok = False
    write_pidfile(tmp_path, 7002)
    spawn_calls = {"n": 0}

    def refuse_spawn() -> FakeHostProcess:
        spawn_calls["n"] += 1
        raise AssertionError("a live host must never be replaced")

    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.terminals.host_manager import TerminalHostManager

    host = TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path), shutdown_grace_seconds=0.2),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
        run_manager=FakeRunManager(),
        connector=_connector_for(client),
        spawner=refuse_spawn,
        pid_identity=lambda _pid: pid_ok,
    )
    token_before = host.ensure_control_token()
    await host.start()

    assert host.adopted is False
    assert host.spawned_this_construction is False
    assert spawn_calls["n"] == 0
    assert host.running is False
    assert host.native_available is False
    assert client.shutdown_calls == []
    assert client.kill_calls == []
    assert (tmp_path / CONTROL_TOKEN_FILE_NAME).read_text() == token_before, "no rotation"
    health = host.health_state()
    assert health["host_mismatch"], health
    assert host.last_error
    assert _loaded(terminals, row.id).state == "live", "rows are not orphaned"

    await host.stop()
    assert client.shutdown_calls == []


def _connector_for(client: FakeControlClient) -> Any:
    async def connect() -> FakeControlClient:
        return client

    return connect


@pytest.mark.asyncio
async def test_drain_stops_a_host_this_daemon_never_adopted(
    tmp_path: Path,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`gobby stop --terminals` is the only path that reaches an unadopted host.

    Adoption closed the daemon's connection, so the drain opens its own — and
    the host refuses every verb until that connection has said `hello` (#22232).
    """
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    client = FakeControlClient(host_epoch=str(uuid.uuid4()), host_pid=7311)
    # The pidfile disagrees with ping.host_pid: the host is alive but not
    # adoptable, which is the state the operator is told to drain.
    write_pidfile(tmp_path, 4111)
    host = _host(tmp_path, terminals, client, pid_ok=True)
    await host.start()
    assert host.adopted is False
    assert host.health_state()["host_mismatch"]

    # A real host hangs up on the refused connection, so the drain's connection
    # starts unauthenticated however the failed adoption ended.
    client.authed = False
    monkeypatch.setattr(host, "_process_alive", lambda _pid: False)
    await host.stop(drain_host=True)

    assert client.authed is True, "the drain must handshake before it commands"
    assert client.shutdown_calls == [200]


@pytest.mark.asyncio
async def test_drain_reports_a_host_that_outlived_the_rpc(
    tmp_path: Path,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain that leaves the host running must not report a clean stop."""
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    client = FakeControlClient(host_epoch=str(uuid.uuid4()), host_pid=7312)
    write_pidfile(tmp_path, 7312)
    host = _host(tmp_path, terminals, client, pid_ok=True)
    await host.start()
    assert host.adopted is True

    monkeypatch.setattr(host, "_process_alive", lambda _pid: True)
    signals: list[int] = []
    monkeypatch.setattr(
        "gobby.terminals.host_manager.os.kill",
        lambda _pid, host_signal: signals.append(host_signal),
    )
    await host.stop(drain_host=True)

    assert client.shutdown_calls == [200]
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert host.last_error == "gterm host 7312 is still running after SIGKILL"


@pytest.mark.asyncio
async def test_adoption_requires_ping_host_pid_proof(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())

    # Matching live pidfile plus matching host_pid adopts.
    client = FakeControlClient(host_epoch=epoch, host_pid=4242)
    write_pidfile(tmp_path, 4242)
    host = _host(tmp_path, terminals, client, pid_ok=True, process=FakeHostProcess(4242))
    await host.start()
    assert host.adopted is True

    # Unrelated live gterm PID (pidfile != ping.host_pid) refuses adoption and
    # degrades instead of spawning over whatever answered on the socket.
    client2 = FakeControlClient(host_epoch=epoch, host_pid=9999)
    write_pidfile(tmp_path, 1111)
    host2 = _host(tmp_path, terminals, client2, pid_ok=True)
    await host2.start()
    assert host2.adopted is False
    assert host2.spawned_this_construction is False
    assert host2.health_state()["host_mismatch"]

    # Nothing answering on the socket spawns a fresh host.
    async def refused() -> FakeControlClient:
        raise ConnectionRefusedError("nobody listening")

    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.terminals.host_manager import TerminalHostManager

    fresh = FakeControlClient(host_epoch=str(uuid.uuid4()), host_pid=4343)
    connects = {"n": 0}

    async def connect_after_spawn() -> FakeControlClient:
        connects["n"] += 1
        if connects["n"] == 1:
            return await refused()
        return fresh

    host3 = TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path), shutdown_grace_seconds=0.2),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
        run_manager=FakeRunManager(),
        connector=connect_after_spawn,
        spawner=lambda: FakeHostProcess(pid=4343),
        pid_identity=lambda pid: pid == 4343,
    )
    await host3.start()
    assert host3.adopted is False
    assert host3.spawned_this_construction is True
    assert host3.host_pid == 4343


@pytest.mark.asyncio
async def test_unknown_host_rechecks_before_kill(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    unknown_id = str(uuid.uuid4())
    spawn_key = "spawn-unknown"
    row = FakeListRow(
        terminal_id=unknown_id,
        spawn_key=spawn_key,
        host_terminal_id="ht-unknown",
        commit_state="committed",
    )
    client = FakeControlClient(terminals=[row], host_pid=5001)
    from gobby.terminals.host_protocol import write_pidfile

    write_pidfile(tmp_path, 5001)
    lookups = {"n": 0}

    class RecheckingManager(TerminalManager):
        def get_by_identity(self, terminal_id: str, spawn_key: str) -> Any:
            lookups["n"] += 1
            found = super().get_by_identity(terminal_id, spawn_key)
            if found is None and terminal_id == unknown_id and lookups["n"] >= 2:
                return self.create_pending(
                    terminal_id=unknown_id,
                    project_id=sample_project["id"],
                    backend="native",
                    ownership="gobby",
                    spawn_key=spawn_key,
                    machine_id=require_machine_id(),
                )
            return found

    manager = RecheckingManager(temp_db)
    host = _host(tmp_path, manager, client)
    await host.start()
    await host.reconcile()
    assert "ht-unknown" not in client.kill_calls


@pytest.mark.asyncio
async def test_adoption_reconciliation_matrix(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    pending_committed = _pending(terminals, sample_project["id"])
    pending_prepared = _pending(terminals, sample_project["id"])
    pending_miss_fresh = _pending(terminals, sample_project["id"])
    live_present = _live(terminals, sample_project["id"], epoch, host_terminal_id="ht-live")
    live_missing = _live(terminals, sample_project["id"], epoch, host_terminal_id="ht-gone")
    old = _live(
        terminals,
        sample_project["id"],
        str(uuid.uuid4()),
        host_terminal_id="ht-old",
    )
    unknown = FakeListRow(
        terminal_id=str(uuid.uuid4()),
        spawn_key="ghost",
        host_terminal_id="ht-ghost",
        commit_state="committed",
    )
    client = FakeControlClient(
        host_epoch=epoch,
        host_pid=6001,
        terminals=[
            FakeListRow(
                terminal_id=pending_committed.id,
                spawn_key=pending_committed.spawn_key or pending_committed.id,
                commit_state="committed",
                host_terminal_id="ht-pc",
            ),
            FakeListRow(
                terminal_id=pending_prepared.id,
                spawn_key=pending_prepared.spawn_key or pending_prepared.id,
                commit_state="prepared",
                observer_bind="reserved",
                host_terminal_id="ht-pp",
                pgid=321,
                start_time=1.0,
            ),
            FakeListRow(
                terminal_id=live_present.id,
                spawn_key=live_present.spawn_key or live_present.id,
                commit_state="committed",
                host_terminal_id="ht-live",
            ),
            unknown,
        ],
    )
    write_pidfile(tmp_path, 6001)
    runs = FakeRunManager()
    host = _host(
        tmp_path,
        terminals,
        client,
        run_manager=runs,
        spawn_in_doubt_seconds=0.01,
    )
    await host.start()
    await host.reconcile()

    assert _loaded(terminals, pending_committed.id).state == "live"
    assert _loaded(terminals, pending_prepared.id).state == "pending"
    # Past in-doubt and missing from host → exited.
    assert _loaded(terminals, pending_miss_fresh.id).state in {"pending", "exited"}
    assert _loaded(terminals, live_present.id).state == "live"
    assert _loaded(terminals, live_missing.id).state == "exited"
    assert _loaded(terminals, old.id).state == "exited"
    assert "ht-ghost" in client.kill_calls

    client.kill_calls.clear()
    with patch.object(
        terminals,
        "list_reconcilable_by_machine",
        side_effect=RuntimeError("db down"),
    ):
        with pytest.raises(RuntimeError, match="db down"):
            await host.reconcile()
    assert client.kill_calls == []


@pytest.mark.asyncio
async def test_reconcile_skips_tmux_observer_slots(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    live = _live(terminals, sample_project["id"], epoch, host_terminal_id="ht-live")
    observer = FakeListRow(
        terminal_id="tmux:/tmp/gobby:86901:1000:%140",
        spawn_key="tmux:/tmp/gobby:86901:1000:%140",
        host_terminal_id="ht-tmux",
        commit_state="committed",
    )
    client = FakeControlClient(
        host_epoch=epoch,
        host_pid=8001,
        terminals=[
            FakeListRow(
                terminal_id=live.id,
                spawn_key=live.spawn_key or live.id,
                commit_state="committed",
                host_terminal_id="ht-live",
            ),
            observer,
        ],
    )
    write_pidfile(tmp_path, 8001)
    host = _host(tmp_path, terminals, client)
    await host.start()
    await host.reconcile()
    assert host.running is True
    assert host.last_error is None
    assert _loaded(terminals, live.id).state == "live"
    assert "ht-tmux" not in client.kill_calls


@pytest.mark.asyncio
async def test_control_token_is_minted_scoped_and_rotated(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    from gobby.terminals.host_protocol import CONTROL_TOKEN_FILE_NAME

    terminals = TerminalManager(temp_db)
    client = FakeControlClient(token="will-be-replaced")
    host = _host(tmp_path, terminals, client)
    token = host.ensure_control_token()
    path = tmp_path / CONTROL_TOKEN_FILE_NAME
    assert path.read_text() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    client.token = token
    from gobby.terminals.host_protocol import write_pidfile

    write_pidfile(tmp_path, client.host_pid)
    await host.start()
    assert host.adopted is True
    same = host.ensure_control_token()
    assert same == token
    rotated = host.rotate_control_token()
    assert rotated != token
    assert path.read_text() == rotated
    health = host.health_state()
    dumped = str(health)
    assert token not in dumped
    assert rotated not in dumped


@pytest.mark.asyncio
async def test_reconcile_does_not_exit_inflight_spawn(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    inflight = _pending(terminals, sample_project["id"])
    epoch = str(uuid.uuid4())
    client = FakeControlClient(host_epoch=epoch, host_pid=7001, terminals=[])
    write_pidfile(tmp_path, 7001)
    host = _host(
        tmp_path,
        terminals,
        client,
        spawn_in_doubt_seconds=150.0,
    )
    await host.start()
    await host.reconcile()
    assert _loaded(terminals, inflight.id).state == "pending"
    assert client.kill_calls == []
    promoted = terminals.promote_to_live(
        inflight.id,
        locator={"host_terminal_id": "ht-late"},
        locator_key=native_locator_key(epoch, "ht-late"),
        host_epoch=epoch,
    )
    assert promoted is not None
    assert promoted.state == "live"

    overdue = _pending(terminals, sample_project["id"])
    host_overdue = _host(
        tmp_path,
        terminals,
        FakeControlClient(host_epoch=epoch, host_pid=7001, terminals=[]),
        spawn_in_doubt_seconds=0.001,
    )
    with patch("gobby.terminals.host_reconcile._age_seconds", return_value=1.0):
        await host_overdue.reconcile()
    assert _loaded(terminals, overdue.id).state == "exited"


@pytest.mark.asyncio
async def test_host_crash_reaps_sighup_ignoring_tree(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    import signal
    import time

    from gobby.terminals.host_protocol import write_pidfile

    def park_until_signalled() -> None:
        # A forked copy of pytest must never hold the runner's stdio: a leaked
        # child that keeps the output pipe open wedges the invoking shell until
        # it dies. Detach, then bound the lifetime so a skipped cleanup cannot
        # leave the tree behind forever.
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        os.close(devnull)
        signal.alarm(30)
        signal.pause()
        os._exit(0)

    def reap_process_group(pgid: int) -> None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pgid, 0)
        except ChildProcessError:
            pass

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    ready_r, ready_w = os.pipe()
    leader = os.fork()
    if leader == 0:
        os.close(ready_r)
        os.setpgid(0, 0)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        child = os.fork()
        if child == 0:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            park_until_signalled()
        os.write(ready_w, b"ok")
        os.close(ready_w)
        park_until_signalled()

    os.close(ready_w)
    try:
        os.read(ready_r, 2)
        os.close(ready_r)
        start_time = time.time()
        row = _pending(terminals, sample_project["id"])
        recorded = terminals.record_process(
            row.id,
            {"host_terminal_id": "ht-tree", "pgid": leader, "start_time": start_time},
            attempt_generation=row.attempt_generation,
            attempt_started_at=row.attempt_started_at,
        )
        assert recorded is not None
        live = terminals.promote_to_live(
            row.id,
            locator={"host_terminal_id": "ht-tree"},
            locator_key=native_locator_key(epoch, "ht-tree"),
            host_epoch=epoch,
        )
        assert live is not None

        client = FakeControlClient(host_epoch=epoch, host_pid=os.getpid())
        write_pidfile(tmp_path, os.getpid())
        host = _host(tmp_path, terminals, client)
        await host.handle_host_death()
        await host.reap_recorded_process({"pgid": os.getpid(), "start_time": 0.0})

        def leader_reaped() -> bool:
            try:
                reaped_pid, _status = os.waitpid(leader, os.WNOHANG)
            except ChildProcessError:
                return True
            return reaped_pid == leader

        wait_for_condition(
            leader_reaped,
            timeout=1.2,
            interval=0.05,
            description="SIGHUP-ignoring process group exit",
        )
        with pytest.raises(ProcessLookupError):
            os.kill(leader, 0)
    finally:
        # Whatever the reaper did, the test owns this process group: kill and
        # reap it so a failed assertion never leaks a SIGHUP-ignoring tree.
        reap_process_group(leader)


@pytest.mark.asyncio
async def test_prepare_commit_persists_before_commit(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    terminals = TerminalManager(temp_db)
    row = _pending(terminals, sample_project["id"])
    client = FakeControlClient()
    host = _host(tmp_path, terminals, client)
    await host.handle_spawn_prepared(
        {
            "terminal_id": row.id,
            "spawn_key": row.spawn_key,
            "pgid": 4242,
            "start_time": 12.5,
            "host_terminal_id": "ht-prep",
        }
    )
    stored = terminals.get(row.id)
    assert stored is not None
    assert stored.state == "pending"
    assert stored.process == {
        "host_terminal_id": "ht-prep",
        "pgid": 4242,
        "start_time": 12.5,
    }
    assert client.spawn_commits == [(row.id, row.spawn_key)]

    failing = _pending(terminals, sample_project["id"])
    with patch.object(terminals, "record_process", return_value=None):
        client.spawn_commits.clear()
        await host.handle_spawn_prepared(
            {
                "terminal_id": failing.id,
                "spawn_key": failing.spawn_key,
                "pgid": 1,
                "start_time": 1.0,
                "host_terminal_id": "ht-fail",
            }
        )
    assert client.spawn_commits == []
    assert _loaded(terminals, failing.id).state == "pending"

    dropped = _pending(terminals, sample_project["id"])
    client.drop_on_commit = True
    client.spawn_commits.clear()
    with pytest.raises(ConnectionError):
        await host.handle_spawn_prepared(
            {
                "terminal_id": dropped.id,
                "spawn_key": dropped.spawn_key,
                "pgid": 9,
                "start_time": 9.0,
                "host_terminal_id": "ht-drop",
            }
        )
    assert _loaded(terminals, dropped.id).state == "pending"


@pytest.mark.asyncio
async def test_reconcile_does_not_promote_uncommitted_prepare(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    row = _pending(terminals, sample_project["id"])
    epoch = str(uuid.uuid4())
    client = FakeControlClient(
        host_epoch=epoch,
        host_pid=8001,
        terminals=[
            FakeListRow(
                terminal_id=row.id,
                spawn_key=row.spawn_key or row.id,
                commit_state="prepared",
                observer_bind="reserved",
                host_terminal_id="ht-prep",
                pgid=88,
                start_time=3.5,
            )
        ],
    )
    write_pidfile(tmp_path, 8001)
    host = _host(tmp_path, terminals, client)
    await host.start()
    await host.reconcile()
    stored = terminals.get(row.id)
    assert stored is not None
    assert stored.state == "pending"
    assert stored.process == {"pgid": 88, "start_time": 3.5}
    assert client.claimed is True


@pytest.mark.asyncio
async def test_observation_state_reconverges_after_daemon_downtime(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    row = _live(terminals, sample_project["id"], epoch)
    client = FakeControlClient(
        host_epoch=epoch,
        host_pid=9300,
        terminals=[
            FakeListRow(
                terminal_id=row.id,
                spawn_key=row.spawn_key or row.id,
                observation_state="orphaned_observation",
                observation_reason="observation_ceiling",
                observation_generation=4,
            )
        ],
    )
    write_pidfile(tmp_path, 9300)
    host = _host(tmp_path, terminals, client)
    await host.start()
    await host.reconcile()
    held = _loaded(terminals, row.id)
    assert held.state == "live"
    assert host.observation_health[row.id]["observation_state"] == "orphaned_observation"
    client.terminals[0].observation_state = "live"
    client.terminals[0].observation_reason = None
    await host.reconcile()
    recovered = _loaded(terminals, row.id)
    assert recovered.state == "live"
    assert host.observation_health[row.id]["observation_state"] == "live"
    host.note_confirmed_absence(row.id)
    exited = _loaded(terminals, row.id)
    assert exited.state == "exited"


def test_gterm_bin_requires_vt_engine() -> None:
    cargo = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "crates" / "gterminal" / "Cargo.toml").read_text()
    )
    bins = cargo.get("bin", [])
    gterm = next(item for item in bins if item.get("name") == "gterm")
    assert gterm.get("required-features") == ["vt-engine"]


def test_host_spawn_forwards_attachment_pool_args(tmp_path: Path) -> None:
    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.terminals.host_manager import TerminalHostManager

    captured: list[list[str]] = []

    class _BoomPopen:
        def __init__(self, args: list[str], **kwargs: object) -> None:
            del kwargs
            captured.append(list(args))
            raise OSError("boom")

    host = TerminalHostManager(
        config=TerminalHostConfig(
            socket_dir=str(tmp_path),
            binary_path="/bin/echo",
            max_attachments_total=8,
            max_attachments_per_terminal=4,
        ),
        terminal_config=TerminalConfig(),
    )
    with patch("subprocess.Popen", _BoomPopen):
        with pytest.raises(OSError, match="boom"):
            host._spawn_host_process()
    argv = captured[0]
    assert argv[argv.index("--max-attachments-total") + 1] == "8"
    assert argv[argv.index("--max-attachments-per-terminal") + 1] == "4"


@pytest.mark.asyncio
async def test_ensure_restart_is_singleflight_with_backoff(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    terminal_manager = TerminalManager(temp_db)
    client = FakeControlClient()
    manager = _host(tmp_path, terminal_manager, client)
    manager._client = None
    manager._stop_requested = False
    controlled = _ControlledSleep()
    manager._sleep = controlled
    spawner = MagicMock(return_value=FakeHostProcess(pid=client.host_pid))
    manager._spawner = spawner

    first = asyncio.create_task(manager.ensure_restart())
    second = asyncio.create_task(manager.ensure_restart())
    await controlled.started.wait()
    controlled.release.set()
    first_epoch, second_epoch = await asyncio.gather(first, second)
    assert first_epoch == client.host_epoch
    assert second_epoch == client.host_epoch
    assert spawner.call_count == 1
    assert controlled.calls == [1.0]
    await manager.stop_producers()

    failure_delays: list[float] = []

    async def instant_sleep(delay: float) -> None:
        failure_delays.append(delay)

    failed = _host(tmp_path, terminal_manager, FakeControlClient())
    failed._client = None
    failed._stop_requested = False
    failed._sleep = instant_sleep
    failed._spawner = MagicMock(side_effect=FileNotFoundError("gterm"))
    with pytest.raises(HostManagerStopped):
        await failed.ensure_restart()
    assert failure_delays == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert failed._spawner.call_count == 5
    assert failed.native_available is False

    ceiling_delays: list[float] = []
    ceiling = _host(tmp_path, terminal_manager, FakeControlClient())
    ceiling.config = ceiling.config.model_copy(
        update={"restart_max_attempts": 7, "restart_backoff_ceiling_seconds": 30.0}
    )
    ceiling._client = None
    ceiling._stop_requested = False

    async def ceiling_sleep(delay: float) -> None:
        ceiling_delays.append(delay)

    ceiling._sleep = ceiling_sleep
    ceiling._spawner = MagicMock(side_effect=FileNotFoundError("gterm"))
    with pytest.raises(HostManagerStopped):
        await ceiling.ensure_restart()
    assert ceiling_delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]

    manager.backoff_seconds = 8.0
    manager._restart_failures = 3
    manager._healthy_since = 0.0
    manager._monotonic = lambda: 61.0
    manager._stop_requested = False
    manager._client = client

    async def stop_after_reconcile() -> None:
        manager._stop_requested = True

    manager.reconcile = AsyncMock(side_effect=stop_after_reconcile)
    manager._sleep = AsyncMock(return_value=None)
    await manager._health_loop()
    assert manager.backoff_seconds == 0.0
    assert manager._restart_failures == 0


@pytest.mark.asyncio
async def test_drained_host_is_never_respawned(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    terminal_manager = TerminalManager(temp_db)
    client = FakeControlClient()
    manager = _host(tmp_path, terminal_manager, client)
    spawner = MagicMock(return_value=FakeHostProcess(pid=client.host_pid))
    manager._spawner = spawner
    manager.host_drained = True

    with pytest.raises(HostManagerStopped):
        await manager.ensure_restart()
    manager.host_drained = False
    manager._stop_requested = True
    with pytest.raises(HostManagerStopped):
        await manager.ensure_restart()
    assert spawner.call_count == 0


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_shared_restart(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    terminal_manager = TerminalManager(temp_db)
    client = FakeControlClient()
    manager = _host(tmp_path, terminal_manager, client)
    manager._client = None
    manager._stop_requested = False
    controlled = _ControlledSleep()
    manager._sleep = controlled
    spawner = MagicMock(return_value=FakeHostProcess(pid=client.host_pid))
    manager._spawner = spawner

    cancelled = asyncio.create_task(manager.ensure_restart())
    survivor = asyncio.create_task(manager.ensure_restart())
    await controlled.started.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    controlled.release.set()
    assert await survivor == client.host_epoch
    assert spawner.call_count == 1
    await manager.stop_producers()


@pytest.mark.asyncio
async def test_stop_fences_restart_creation_and_publication(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    terminal_manager = TerminalManager(temp_db)
    client = FakeControlClient()
    manager = _host(tmp_path, terminal_manager, client)
    manager._client = None
    manager._stop_requested = False
    controlled = _ControlledSleep()
    manager._sleep = controlled
    spawner = MagicMock(return_value=FakeHostProcess(pid=client.host_pid))
    manager._spawner = spawner
    manager.rotate_control_token = MagicMock(return_value="rotated")

    waiting = asyncio.create_task(manager.ensure_restart())
    await controlled.started.wait()
    await manager.stop()
    with pytest.raises(HostManagerStopped):
        await waiting
    with pytest.raises(HostManagerStopped):
        await manager.ensure_restart()
    assert spawner.call_count == 0
    assert manager.rotate_control_token.call_count == 0
    assert manager._client is None

    await manager.start()
    manager._client = None

    # start() found no pidfile, so it armed the health loop on the mismatch
    # path (#22337); its ticks must yield or they starve ensure_restart.
    async def yielding(delay: float) -> None:
        del delay
        await asyncio.sleep(0)

    manager._sleep = yielding
    assert await manager.ensure_restart() == client.host_epoch
    assert spawner.call_count == 1
    await manager.stop_producers()


# --- adoption retry after a mismatch at start (#22337) ---------------------

_WIRE_GOLDEN = (
    Path(__file__).resolve().parents[2]
    / "crates"
    / "gterminal"
    / "tests"
    / "fixtures"
    / "wire_golden"
)


class _Ticks:
    """Health-loop sleep double: run `before_tick` ahead of each tick, then park.

    After `count` ticks the loop parks on an event that is never set and `done`
    is set, so a test asserts on settled state before `stop()` cancels the loop.
    """

    def __init__(self, count: int, before_tick: Callable[[int], None] | None = None) -> None:
        self.count = count
        self.before_tick = before_tick
        self.calls = 0
        self.done = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        del delay
        self.calls += 1
        if self.calls > self.count:
            self.done.set()
            await asyncio.Event().wait()
        if self.before_tick is not None:
            self.before_tick(self.calls)


class _NullWriter:
    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


async def test_mismatch_retries_and_adopts_after_pidfile_recovers(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    """A stale pidfile at start mismatches; the next health tick adopts in place."""
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    client = FakeControlClient(host_epoch=epoch, host_pid=4242)
    write_pidfile(tmp_path, 1111)
    host = _host(tmp_path, terminals, client, pid_ok=True, process=FakeHostProcess(4242))

    def pidfile_recovers(tick: int) -> None:
        if tick == 1:
            write_pidfile(tmp_path, 4242)

    ticks = _Ticks(count=2, before_tick=pidfile_recovers)
    host._sleep = ticks

    await host.start()
    assert host.adopted is False
    assert host.native_available is False
    assert host.host_epoch is None
    assert host._health_task is not None, "a mismatch must arm the health loop"

    await asyncio.wait_for(ticks.done.wait(), 5)
    assert host.adopted is True
    assert host.native_available is True
    assert host.running is True
    assert host.host_epoch == epoch
    assert host.host_pid == 4242
    assert host.host_mismatch is None
    assert host.spawned_this_construction is False
    await host.stop()


async def test_mismatch_retry_never_spawns_second_host(
    tmp_path: Path,
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mismatch persisting across ticks leaves the live host untouched."""
    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.terminals.host_manager import TerminalHostManager
    from gobby.terminals.host_protocol import CONTROL_TOKEN_FILE_NAME, write_pidfile

    terminals = TerminalManager(temp_db)
    client = FakeControlClient(host_epoch=str(uuid.uuid4()), host_pid=4242)
    write_pidfile(tmp_path, 1111)
    spawn_calls = {"n": 0}

    def refuse_spawn() -> FakeHostProcess:
        spawn_calls["n"] += 1
        raise AssertionError("a live host must never be replaced")

    host = TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path), shutdown_grace_seconds=0.2),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
        run_manager=FakeRunManager(),
        connector=_connector_for(client),
        spawner=refuse_spawn,
        pid_identity=lambda _pid: True,
    )
    token_before = host.ensure_control_token()
    ticks = _Ticks(count=4)
    host._sleep = ticks

    with caplog.at_level(logging.WARNING):
        await host.start()
        await asyncio.wait_for(ticks.done.wait(), 5)

    assert ticks.calls == 5, "four retries ran on the health interval"
    assert spawn_calls["n"] == 0
    assert host.adopted is False
    assert host.spawned_this_construction is False
    assert host.native_available is False
    assert host.host_epoch is None
    assert host.health_state()["host_mismatch"]
    assert client.shutdown_calls == []
    assert client.kill_calls == []
    assert (tmp_path / CONTROL_TOKEN_FILE_NAME).read_text() == token_before, "no rotation"
    reported = [r for r in caplog.records if "not adoptable" in r.getMessage()]
    assert len(reported) == 1, "an unchanged mismatch is reported once, not every tick"
    await host.stop()
    assert client.shutdown_calls == []


async def test_mismatch_warning_does_not_advise_draining_terminals(
    tmp_path: Path,
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    client = FakeControlClient(host_epoch=str(uuid.uuid4()), host_pid=4242)
    write_pidfile(tmp_path, 1111)
    host = _host(tmp_path, terminals, client, pid_ok=True)

    with caplog.at_level(logging.WARNING):
        await host.start()

    messages = [r.getMessage() for r in caplog.records if "not adoptable" in r.getMessage()]
    assert messages, caplog.text
    for message in messages:
        assert "stop --terminals" not in message
        assert "retried" in message
        assert "gobby restart" in message
    await host.stop()


async def test_stale_pidfile_at_start_recovers_without_restart(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    """The 2026-09-14 07:10 outage end to end, minus the daemon restart it needed."""
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    # welcome.bin carries host epoch "epoch-1"; the live host reports the same.
    client = FakeControlClient(host_epoch="epoch-1", host_pid=4242)
    write_pidfile(tmp_path, 1111)
    host = _host(tmp_path, terminals, client, pid_ok=True, process=FakeHostProcess(4242))

    def pidfile_recovers(tick: int) -> None:
        if tick == 1:
            write_pidfile(tmp_path, 4242)

    ticks = _Ticks(count=1, before_tick=pidfile_recovers)
    host._sleep = ticks

    await host.start()
    assert host.adopted is False
    assert host.host_epoch is None

    await asyncio.wait_for(ticks.done.wait(), 5)
    assert host.adopted is True
    assert host.native_available is True
    assert host.host_epoch == "epoch-1"

    incoming = asyncio.StreamReader()
    incoming.feed_data((_WIRE_GOLDEN / "welcome.bin").read_bytes())
    frame = FrameClient(incoming, cast(Any, _NullWriter()))
    locator = AttachLocator(backend="native", frame_host_epoch="epoch-1", host_terminal_id="ht-1")
    await frame.handshake(locator, local_token="token")
    assert frame.closed is False
    await host.stop()


async def test_stale_control_socket_spawns_host(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    """A socket file nobody listens on spawns a host instead of degrading (#22425)."""
    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.terminals.host_client import HostUnavailableError
    from gobby.terminals.host_manager import TerminalHostManager
    from gobby.terminals.host_protocol import control_socket_path

    # The file a host that died without unlinking its socket leaves behind.
    control_socket_path(tmp_path).touch()
    terminals = TerminalManager(temp_db)
    fresh = FakeControlClient(host_epoch=str(uuid.uuid4()), host_pid=4343)
    spawns: list[FakeHostProcess] = []
    connects = {"n": 0}

    async def connect_after_spawn() -> FakeControlClient:
        connects["n"] += 1
        if connects["n"] == 1:
            # What HostClient.connect raises for a refused connection.
            raise HostUnavailableError("gterm host unavailable")
        return fresh

    def spawn() -> FakeHostProcess:
        process = FakeHostProcess(pid=4343)
        spawns.append(process)
        return process

    host = TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path), shutdown_grace_seconds=0.2),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
        run_manager=FakeRunManager(),
        connector=connect_after_spawn,
        spawner=spawn,
        pid_identity=lambda pid: pid == 4343,
    )

    await host.start()

    assert spawns, "a stale control socket must not abort the spawn"
    assert host.running is True
    assert host.native_available is True
    assert host.adopted is False
    assert host.spawned_this_construction is True
    assert host.host_pid == 4343
    await host.stop()


async def test_spawn_failure_retries_from_health_loop(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    """A start that spawned nothing recovers on a health tick, not a restart (#22425)."""
    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.terminals.host_client import HostUnavailableError
    from gobby.terminals.host_manager import TerminalHostManager

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    client = FakeControlClient(host_epoch=epoch, host_pid=4242)
    binary_installed = False
    spawns: list[FakeHostProcess] = []

    async def connect() -> FakeControlClient:
        # Nothing answers the control socket until this manager spawns a host.
        if not spawns:
            raise HostUnavailableError("gterm host unavailable")
        return client

    def spawn() -> FakeHostProcess:
        if not binary_installed:
            raise FileNotFoundError("gterm")
        process = FakeHostProcess(pid=4242)
        spawns.append(process)
        return process

    host = TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path), shutdown_grace_seconds=0.2),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
        run_manager=FakeRunManager(),
        connector=connect,
        spawner=spawn,
        pid_identity=lambda pid: pid == 4242,
    )

    def binary_appears(tick: int) -> None:
        nonlocal binary_installed
        if tick == 1:
            binary_installed = True

    ticks = _Ticks(count=1, before_tick=binary_appears)
    host._sleep = ticks

    await host.start()
    assert host.running is False
    assert spawns == []
    assert host.last_error
    assert host._health_task is not None, "a failed spawn must arm the health loop"

    await asyncio.wait_for(ticks.done.wait(), 5)

    assert len(spawns) == 1
    assert host.running is True
    assert host.native_available is True
    assert host.host_epoch == epoch
    assert host.host_pid == 4242
    assert host.last_error is None
    await host.stop()
