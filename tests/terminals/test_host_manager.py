"""Daemon supervision of gterm (plan 3.1 host manager)."""

from __future__ import annotations

import asyncio
import os
import stat
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.config.terminals import TerminalConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from gobby.utils.machine_id import require_machine_id
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
async def test_adoption_rejects_mismatch_and_stop_drains(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    stale_epoch = str(uuid.uuid4())
    row = _live(terminals, sample_project["id"], stale_epoch)
    stale = FakeControlClient(host_epoch=stale_epoch, host_pid=1)
    write_pidfile(tmp_path, 1)
    replacement = FakeControlClient(host_epoch=str(uuid.uuid4()), host_pid=9001)
    spawned = FakeHostProcess(pid=9001)
    connect_calls = {"n": 0}

    async def connect() -> FakeControlClient:
        connect_calls["n"] += 1
        if connect_calls["n"] == 1:
            return stale
        return replacement

    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.terminals.host_manager import TerminalHostManager

    host = TerminalHostManager(
        config=TerminalHostConfig(socket_dir=str(tmp_path), shutdown_grace_seconds=0.2),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
        run_manager=FakeRunManager(),
        connector=connect,
        spawner=lambda: spawned,
        pid_identity=lambda pid: pid == 9001,
    )
    await host.start()
    assert stale.shutdown_calls, "stale host must be drained before replacement"
    assert host.spawned_this_construction is True
    updated = terminals.get(row.id)
    assert updated is not None
    assert updated.state == "orphaned"

    await host.stop(preserve_host=False)
    assert replacement.shutdown_calls, "full stop drains the replacement host"
    await host.stop(preserve_host=True)
    assert len(replacement.shutdown_calls) == 1, "planned restart must not drain"


@pytest.mark.asyncio
async def test_adoption_requires_ping_host_pid_proof(
    tmp_path: Path,
    temp_db: HubDatabase,
) -> None:
    from gobby.terminals.host_protocol import write_pidfile

    terminals = TerminalManager(temp_db)
    epoch = str(uuid.uuid4())
    client = FakeControlClient(host_epoch=epoch, host_pid=4242)

    # Stale pidfile (dead pid) refuses adoption.
    write_pidfile(tmp_path, 4242)
    host = _host(tmp_path, terminals, client, pid_ok=False)
    await host.start()
    assert host.adopted is False
    assert host.spawned_this_construction is True

    # Matching live pidfile plus matching host_pid adopts.
    client2 = FakeControlClient(host_epoch=epoch, host_pid=4242)
    write_pidfile(tmp_path, 4242)
    host2 = _host(tmp_path, terminals, client2, pid_ok=True, process=FakeHostProcess(4242))
    await host2.start()
    assert host2.adopted is True

    # Unrelated live gterm PID (pidfile != ping.host_pid) refuses.
    client3 = FakeControlClient(host_epoch=epoch, host_pid=9999)
    write_pidfile(tmp_path, 1111)
    host3 = _host(tmp_path, terminals, client3, pid_ok=True)
    await host3.start()
    assert host3.adopted is False


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
    await asyncio.sleep(0.02)
    host_overdue = _host(
        tmp_path,
        terminals,
        FakeControlClient(host_epoch=epoch, host_pid=7001, terminals=[]),
        spawn_in_doubt_seconds=0.001,
    )
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
            time.sleep(30)
            os._exit(0)
        os.write(ready_w, b"ok")
        os.close(ready_w)
        time.sleep(30)
        os._exit(0)

    os.close(ready_w)
    os.read(ready_r, 2)
    os.close(ready_r)
    start_time = time.time()
    row = _pending(terminals, sample_project["id"])
    recorded = terminals.record_process(
        row.id,
        {"pgid": leader, "start_time": start_time},
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
    deadline = time.time() + 1.2
    while time.time() < deadline:
        try:
            os.kill(leader, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            break
    try:
        os.waitpid(leader, os.WNOHANG)
    except ChildProcessError:
        pass
    with pytest.raises(ProcessLookupError):
        os.kill(leader, 0)


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
    assert stored.process == {"pgid": 4242, "start_time": 12.5}
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


def test_gterm_bin_requires_vt_engine() -> None:
    cargo = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "crates" / "gterminal" / "Cargo.toml").read_text()
    )
    bins = cargo.get("bin", [])
    gterm = next(item for item in bins if item.get("name") == "gterm")
    assert gterm.get("required-features") == ["vt-engine"]
