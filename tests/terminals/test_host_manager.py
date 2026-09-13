"""Daemon supervision of gterm (plan 3.1 host manager)."""

from __future__ import annotations

import asyncio
import os
import signal
import stat
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.config.terminals import TerminalConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from gobby.terminals.host_client import HostManagerStopped
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
    assert updated.state == "orphaned"
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
    assert _loaded(terminals, old.id).state == "orphaned"
    assert "ht-ghost" in client.kill_calls

    client.kill_calls.clear()
    with patch.object(terminals, "list_live_by_machine", side_effect=RuntimeError("db down")):
        await host.reconcile()
    assert client.kill_calls == []
    assert host.last_error


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
        host.reap_recorded_process({"pgid": os.getpid(), "start_time": 0.0})

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
    manager._sleep = AsyncMock(return_value=None)
    assert await manager.ensure_restart() == client.host_epoch
    assert spawner.call_count == 1
    await manager.stop_producers()
