"""Upgrade trigger and handover window in TerminalHostManager (plan gterm-host-handover 2.1)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from gobby.terminals.host_client import (
    HelloResult,
    HostCommandError,
    HostConnectionLost,
    PingResult,
    UpgradeOutcome,
    UpgradeStatus,
    parse_ping,
)
from gobby.terminals.host_manager import TerminalHostManager
from gobby.terminals.host_protocol import write_pidfile
from gobby.terminals.host_upgrade import HOST_BUDGET_SECONDS, REQUEST_TIMEOUT_SECONDS
from gobby.utils.machine_id import require_machine_id
from tests.terminals.host_fakes import FakeHostProcess, FakeListRow, FakeRunManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
INTERVAL = 1.0
HOST_PID = 4242
IDLE = UpgradeStatus(phase="idle")


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _install(path: Path, content: bytes) -> str:
    """Promote ``content`` over ``path`` by new inode, as installs do; return its hash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    staged.write_bytes(content)
    os.replace(staged, path)
    return hashlib.sha256(content).hexdigest()


class _Conn:
    """One control connection to a `_Host`; an exec drops it."""

    def __init__(self, host: _Host) -> None:
        self.host = host
        self.dead = False

    def _require_live(self) -> _Host:
        if self.dead:
            raise HostConnectionLost("gterm control connection lost")
        return self.host

    async def hello(self, protocol_version: int, control_token: str) -> HelloResult:
        del control_token
        host = self._require_live()
        host.hellos += 1
        return HelloResult(
            host_epoch=host.host_epoch,
            version="0.1.3",
            protocol_version=protocol_version,
            capabilities=host.capabilities,
        )

    async def ping(self) -> PingResult:
        host = self._require_live()
        return PingResult(
            host_epoch=host.host_epoch,
            version="0.1.3",
            host_pid=host.host_pid,
            binary_sha256=host.binary_sha256,
            generation=host.generation,
            upgrade=host.upgrade if "host_upgrade" in host.capabilities else None,
        )

    async def list_terminals(self) -> list[FakeListRow]:
        host = self._require_live()
        host.lists += 1
        return list(host.terminals)

    async def kill(self, host_terminal_id: str) -> None:
        raise AssertionError(f"an upgrade must not kill {host_terminal_id}")

    async def host_upgrade(self, exe: str, attempt_id: str) -> dict[str, Any]:
        host = self._require_live()
        host.requests.append((exe, attempt_id))
        if host.reply is None:
            raise AssertionError("this host must not be asked to upgrade")
        return host.reply(attempt_id)

    async def close(self) -> None:
        self.dead = True


@dataclass
class _Host:
    """A gterm host that reports its running image and serves `host_upgrade`."""

    binary_sha256: str
    host_epoch: str = field(default_factory=lambda: str(uuid.uuid4()))
    host_pid: int = HOST_PID
    capabilities: tuple[str, ...] = ("terminal_theme", "host_upgrade")
    generation: int = 0
    upgrade: UpgradeStatus = IDLE
    terminals: list[FakeListRow] = field(default_factory=list)
    reply: Callable[[str], dict[str, Any]] | None = None
    accepting: bool = True
    connections: list[_Conn] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)
    hellos: int = 0
    lists: int = 0
    refused_connects: int = 0

    async def connect(self) -> _Conn:
        if not self.accepting:
            self.refused_connects += 1
            raise ConnectionRefusedError("gterm host is restoring")
        conn = _Conn(self)
        self.connections.append(conn)
        return conn

    def exec_image(self) -> None:
        """The old image execs: open connections drop, the new one is not listening yet."""
        for conn in self.connections:
            conn.dead = True
        self.accepting = False

    def accept(self, attempt_id: str, candidate: str, remaining_ms: int = 15_000) -> dict[str, Any]:
        self.upgrade = UpgradeStatus(
            phase="probing",
            attempt_id=attempt_id,
            candidate_sha256=candidate,
            remaining_ms=remaining_ms,
        )
        return {
            "ok": True,
            "accepted": True,
            "attempt_id": attempt_id,
            "candidate_sha256": candidate,
            "remaining_ms": remaining_ms,
            "generation": self.generation,
        }

    def in_progress(self, attempt_id: str, candidate: str, phase: str, remaining_ms: int) -> None:
        self.upgrade = UpgradeStatus(
            phase=phase,
            attempt_id=attempt_id,
            candidate_sha256=candidate,
            remaining_ms=remaining_ms,
        )

    def finish(
        self,
        attempt_id: str,
        outcome: str,
        candidate: str | None,
        *,
        running: str | None = None,
    ) -> None:
        self.upgrade = UpgradeStatus(
            phase="idle",
            last_outcome=UpgradeOutcome(
                attempt_id=attempt_id, outcome=outcome, candidate_sha256=candidate
            ),
        )
        if running is not None:
            self.binary_sha256 = running
            self.generation += 1


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Ticks:
    """Health-loop sleep double: `run()` releases whole ticks one at a time."""

    def __init__(self) -> None:
        self._parked = asyncio.Event()
        self._go = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        del delay
        self._parked.set()
        await self._go.wait()
        self._go.clear()

    async def run(self, count: int = 1) -> None:
        for _ in range(count):
            await asyncio.wait_for(self._parked.wait(), 5)
            self._parked.clear()
            self._go.set()
            await asyncio.wait_for(self._parked.wait(), 5)


@dataclass
class _Rig:
    manager: TerminalHostManager
    ticks: _Ticks
    clock: _Clock
    alive: set[int]
    spawns: list[FakeHostProcess]


def _rig(tmp_path: Path, terminals: TerminalManager, host: _Host, exe: Path) -> _Rig:
    socket_dir = tmp_path / "sock"
    socket_dir.mkdir(parents=True, exist_ok=True)
    write_pidfile(socket_dir, host.host_pid)
    alive = {host.host_pid}
    spawns: list[FakeHostProcess] = []

    def spawn() -> FakeHostProcess:
        process = FakeHostProcess(pid=host.host_pid)
        spawns.append(process)
        return process

    manager = TerminalHostManager(
        config=TerminalHostConfig(
            enabled=True,
            socket_dir=str(socket_dir),
            binary_path=str(exe),
            shutdown_grace_seconds=0.2,
            health_interval_seconds=INTERVAL,
        ),
        terminal_config=TerminalConfig(),
        terminal_manager=terminals,
        run_manager=FakeRunManager(),
        connector=host.connect,
        spawner=spawn,
        pid_identity=lambda pid: pid in alive,
    )
    ticks = _Ticks()
    clock = _Clock()
    manager._sleep = ticks
    manager._monotonic = clock
    return _Rig(manager=manager, ticks=ticks, clock=clock, alive=alive, spawns=spawns)


def _live_rows(
    terminals: TerminalManager, project_id: str, host: _Host, refs: tuple[str, ...]
) -> list[Terminal]:
    """Live native rows on ``host``'s epoch, each listed by the host."""
    rows: list[Terminal] = []
    for ref in refs:
        tid = str(uuid.uuid4())
        terminals.create_pending(
            terminal_id=tid,
            project_id=project_id,
            backend="native",
            ownership="gobby",
            spawn_key=tid,
            machine_id=require_machine_id(),
        )
        promoted = terminals.promote_to_live(
            tid,
            locator={"host_terminal_id": ref},
            locator_key=native_locator_key(host.host_epoch, ref),
            host_epoch=host.host_epoch,
        )
        assert promoted is not None
        rows.append(promoted)
        host.terminals.append(FakeListRow(terminal_id=tid, spawn_key=tid, host_terminal_id=ref))
    return rows


def _identity(terminals: TerminalManager, rows: list[Terminal]) -> list[tuple[Any, ...]]:
    loaded = [terminals.get(row.id) for row in rows]
    return [
        (row.host_epoch, row.locator_key, row.state, row.agent_run_id)
        for row in loaded
        if row is not None
    ]


async def _attach_ready(manager: TerminalHostManager) -> bool:
    """What the terminal WebSocket asks before resolving an attach locator."""
    return await manager.wait_startup_settled(0.01)


@pytest.mark.asyncio
async def test_stale_host_gets_one_upgrade_request(tmp_path: Path, temp_db: HubDatabase) -> None:
    """2.1.1: a stale adopted host is sent one host_upgrade; a current host none."""
    exe = tmp_path / "bin" / "gterm"
    installed = _install(exe, b"gterm 0.1.4")
    stale = _Host(binary_sha256="a" * 64)
    stale.reply = lambda attempt_id: stale.accept(attempt_id, installed)
    rig = _rig(tmp_path / "stale", TerminalManager(temp_db), stale, exe)

    await rig.manager.start()
    assert len(stale.requests) == 1
    sent_exe, attempt_id = stale.requests[0]
    assert sent_exe == str(exe)
    assert uuid.UUID(hex=attempt_id).hex == attempt_id

    stale.in_progress(attempt_id, installed, "capturing", 9_000)
    await rig.ticks.run(2)
    stale.finish(attempt_id, "succeeded", installed, running=installed)
    await rig.ticks.run(2)
    assert stale.requests == [(str(exe), attempt_id)]
    await rig.manager.stop()

    current = _Host(binary_sha256=installed)
    rig = _rig(tmp_path / "current", TerminalManager(temp_db), current, exe)
    await rig.manager.start()
    await rig.ticks.run(3)
    assert current.requests == []
    await rig.manager.stop()


@pytest.mark.asyncio
async def test_window_holds_rows_until_terminal_outcome(
    tmp_path: Path, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """2.1.2: failed pings and reconnects hold host death, restart, and reconcile."""
    terminals = TerminalManager(temp_db)
    exe = tmp_path / "bin" / "gterm"
    installed = _install(exe, b"gterm 0.1.4")
    host = _Host(binary_sha256="a" * 64)
    rows = _live_rows(terminals, sample_project["id"], host, ("ht-1",))
    before = _identity(terminals, rows)
    host.reply = lambda attempt_id: host.accept(attempt_id, installed)
    rig = _rig(tmp_path, terminals, host, exe)
    deaths = AsyncMock()

    with patch.object(rig.manager, "handle_host_death", new=deaths):
        await rig.manager.start()
        attempt_id = host.requests[0][1]
        assert await _attach_ready(rig.manager) is False

        # A healthy ping from the old image mid-quiesce keeps the window open.
        host.in_progress(attempt_id, installed, "quiescing", 12_000)
        await rig.ticks.run()
        assert await _attach_ready(rig.manager) is False

        # The exec drops the connection; the reconnect to the live pid fails.
        host.exec_image()
        await rig.ticks.run()
        assert host.refused_connects == 1
        # The pid is gone too: still held.
        rig.alive.clear()
        await rig.ticks.run()
        deaths.assert_not_awaited()

        restart = asyncio.create_task(rig.manager.ensure_restart())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not restart.done(), "a restart must wait for the window"
        assert rig.spawns == []
        restart.cancel()
        with pytest.raises(asyncio.CancelledError):
            await restart

        assert host.lists == 0, "no reconcile runs while the window is open"
        assert _identity(terminals, rows) == before
        assert await _attach_ready(rig.manager) is False

        rig.clock.now += 16.5
        await rig.ticks.run()
        deaths.assert_awaited_once()
    await rig.manager.stop()


@pytest.mark.asyncio
async def test_failed_candidates_are_not_retried(tmp_path: Path, temp_db: HubDatabase) -> None:
    """2.1.3: every failed outcome refuses the candidate until the install changes."""
    exe = tmp_path / "bin" / "gterm"
    running = _install(exe, b"gterm 0.1.3")
    host = _Host(binary_sha256=running)
    rig = _rig(tmp_path, TerminalManager(temp_db), host, exe)
    await rig.manager.start()
    await rig.ticks.run()
    assert host.requests == []

    def upgrade_refused(_attempt_id: str) -> dict[str, Any]:
        raise HostCommandError("upgrade_refused", detail="probe exited 1")

    finishes: list[tuple[str, str | None]] = [
        ("refused", None),
        ("rolled_back", None),
        ("aborted", None),
        # A forced restore failure: the new image fell back to the old one.
        ("fallback", None),
        # Identity failure: `succeeded`, but the host runs some other image.
        ("succeeded", "f" * 64),
    ]
    for build, (outcome, runs) in enumerate(finishes):
        candidate = _install(exe, f"gterm build {build}".encode())
        host.reply = partial(host.accept, candidate=candidate)
        await rig.ticks.run()
        assert len(host.requests) == build + 1
        host.finish(host.requests[-1][1], outcome, candidate, running=runs)
        await rig.ticks.run(4)
        assert len(host.requests) == build + 1, f"{outcome} must not be retried"
        assert await _attach_ready(rig.manager) is True

    # An `upgrade_refused` answer names no candidate: the installed hash is refused.
    _install(exe, b"gterm refused at probe")
    host.reply = upgrade_refused
    await rig.ticks.run(4)
    assert len(host.requests) == len(finishes) + 1
    assert await _attach_ready(rig.manager) is True
    await rig.manager.stop()


@pytest.mark.asyncio
async def test_pre_handover_host_is_left_alone(
    tmp_path: Path, temp_db: HubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    """2.1.4: a host without the capability is never sent the verb; one warning."""
    exe = tmp_path / "bin" / "gterm"
    _install(exe, b"gterm 0.1.4")
    host = _Host(binary_sha256="a" * 64, capabilities=("terminal_theme",))
    rig = _rig(tmp_path, TerminalManager(temp_db), host, exe)

    with caplog.at_level(logging.WARNING):
        await rig.manager.start()
        await rig.ticks.run(3)
    assert host.requests == []
    warnings = [
        r.getMessage() for r in caplog.records if "gobby restart --terminals" in r.getMessage()
    ]
    assert len(warnings) == 1, caplog.text
    assert str(HOST_PID) in warnings[0]
    await rig.manager.stop()


@pytest.mark.asyncio
async def test_successful_upgrade_changes_no_rows(
    tmp_path: Path, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """2.1.5: `succeeded` with the candidate closes the window; reconcile keeps rows."""
    terminals = TerminalManager(temp_db)
    exe = tmp_path / "bin" / "gterm"
    installed = _install(exe, b"gterm 0.1.4")
    host = _Host(binary_sha256="a" * 64)
    rows = _live_rows(terminals, sample_project["id"], host, ("ht-1", "ht-2"))
    before = _identity(terminals, rows)
    host.reply = lambda attempt_id: host.accept(attempt_id, installed)
    rig = _rig(tmp_path, terminals, host, exe)
    deaths = AsyncMock()

    with patch.object(rig.manager, "handle_host_death", new=deaths):
        await rig.manager.start()
        attempt_id = host.requests[0][1]
        host.exec_image()
        await rig.ticks.run()
        host.accepting = True
        host.finish(attempt_id, "succeeded", installed, running=installed)
        await rig.ticks.run()
        assert await _attach_ready(rig.manager) is True
        lists = host.lists
        await rig.ticks.run()
        assert host.lists > lists, "the closed window reconciles again"
    deaths.assert_not_awaited()
    assert _identity(terminals, rows) == before
    assert rig.manager.host_epoch == host.host_epoch
    assert host.requests == [(str(exe), attempt_id)]
    await rig.manager.stop()


@pytest.mark.asyncio
async def test_window_follows_host_attempt_record(tmp_path: Path, temp_db: HubDatabase) -> None:
    """2.1.6: the window keys to the daemon's attempt_id and the host's candidate."""
    exe = tmp_path / "bin" / "gterm"

    # A lost acceptance reply: the host runs the attempt, the answer never lands.
    daemon_sees = _install(exe, b"gterm lost ack")
    lost = _Host(binary_sha256="a" * 64)

    def lose_reply(attempt_id: str) -> dict[str, Any]:
        lost.accept(attempt_id, daemon_sees)
        raise HostConnectionLost("reply lost")

    lost.reply = lose_reply
    rig = _rig(tmp_path / "lost", TerminalManager(temp_db), lost, exe)
    await rig.manager.start()
    window = rig.manager.upgrade.window
    assert window is not None
    assert window.attempt_id == lost.requests[0][1]
    assert window.confirmed is False
    await rig.ticks.run()
    assert window.confirmed is True
    assert window.candidate_sha256 == daemon_sees
    await rig.manager.stop()

    # A daemon restart mid-window: the new daemon adopts the host's attempt.
    pinned_by_host = _install(exe, b"gterm daemon restart")
    restarted = _Host(binary_sha256="b" * 64)
    restarted.in_progress("feedface" * 4, pinned_by_host, "capturing", 6_000)
    rig = _rig(tmp_path / "restart", TerminalManager(temp_db), restarted, exe)
    await rig.manager.start()
    window = rig.manager.upgrade.window
    assert window is not None
    assert (window.attempt_id, window.candidate_sha256) == ("feedface" * 4, pinned_by_host)
    assert restarted.requests == []
    assert await _attach_ready(rig.manager) is False
    await rig.manager.stop()

    # A promotion lands between the daemon's hash and the host's pin.
    hashed = _install(exe, b"gterm hashed by the daemon")
    raced = _Host(binary_sha256="c" * 64)
    pinned: list[str] = []

    def promote_then_accept(attempt_id: str) -> dict[str, Any]:
        pinned.append(_install(exe, b"gterm promoted before the pin"))
        return raced.accept(attempt_id, pinned[0])

    raced.reply = promote_then_accept
    rig = _rig(tmp_path / "raced", TerminalManager(temp_db), raced, exe)
    await rig.manager.start()
    window = rig.manager.upgrade.window
    assert window is not None
    assert window.attempt_id == raced.requests[0][1]
    assert (window.sha_at_start, window.candidate_sha256) == (hashed, pinned[0])
    raced.finish(window.attempt_id, "succeeded", pinned[0], running=pinned[0])
    await rig.ticks.run(3)
    assert rig.manager.upgrade.window is None
    assert len(raced.requests) == 1, "the pinned image is current, not an identity failure"
    await rig.manager.stop()


@pytest.mark.asyncio
async def test_lost_ack_and_failed_reconnects_keep_rows(
    tmp_path: Path, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """2.1.7: a lost ack and two failed reconnects end on `succeeded`, rows intact."""
    terminals = TerminalManager(temp_db)
    exe = tmp_path / "bin" / "gterm"
    installed = _install(exe, b"gterm 0.1.4")
    host = _Host(binary_sha256="a" * 64)
    rows = _live_rows(terminals, sample_project["id"], host, ("ht-1",))
    before = _identity(terminals, rows)

    def exec_before_reply(attempt_id: str) -> dict[str, Any]:
        host.accept(attempt_id, installed)
        host.exec_image()
        raise HostConnectionLost("gterm control connection lost")

    host.reply = exec_before_reply
    rig = _rig(tmp_path, terminals, host, exe)
    deaths = AsyncMock()

    with patch.object(rig.manager, "handle_host_death", new=deaths):
        await rig.manager.start()
        attempt_id = host.requests[0][1]
        await rig.ticks.run(2)
        assert host.refused_connects == 2
        deaths.assert_not_awaited()
        assert await _attach_ready(rig.manager) is False

        host.accepting = True
        host.finish(attempt_id, "succeeded", installed, running=installed)
        await rig.ticks.run()
        assert await _attach_ready(rig.manager) is True
        await rig.ticks.run()
    deaths.assert_not_awaited()
    assert _identity(terminals, rows) == before
    assert host.requests == [(str(exe), attempt_id)]
    await rig.manager.stop()


@pytest.mark.asyncio
async def test_unrun_request_closes_only_on_fresh_check(
    tmp_path: Path, temp_db: HubDatabase
) -> None:
    """2.1.8: an unrun request closes only on a fresh post-deadline hello and ping."""
    exe = tmp_path / "bin" / "gterm"
    _install(exe, b"gterm 0.1.4")
    host = _Host(binary_sha256="a" * 64)

    def lost_in_transit(_attempt_id: str) -> dict[str, Any]:
        raise HostConnectionLost("request lost in transit")

    host.reply = lost_in_transit
    rig = _rig(tmp_path, TerminalManager(temp_db), host, exe)
    deaths = AsyncMock()

    with patch.object(rig.manager, "handle_host_death", new=deaths):
        await rig.manager.start()
        first = host.requests[0][1]
        # Idle pings before the deadline do not close the window.
        await rig.ticks.run(2)
        assert await _attach_ready(rig.manager) is False
        assert len(host.requests) == 1

        hellos = host.hellos
        rig.clock.now += REQUEST_TIMEOUT_SECONDS + HOST_BUDGET_SECONDS + INTERVAL + 0.5
        await rig.ticks.run()
        assert host.hellos == hellos + 1, "the close needs a fresh hello and ping"
        assert await _attach_ready(rig.manager) is True
        deaths.assert_not_awaited()

        # The next tick asks again; this time the host is gone at the deadline.
        await rig.ticks.run()
        assert len(host.requests) == 2
        assert host.requests[1][1] != first
        host.exec_image()
        rig.clock.now += REQUEST_TIMEOUT_SECONDS + HOST_BUDGET_SECONDS + INTERVAL + 0.5
        await rig.ticks.run()
        deaths.assert_awaited_once()
    await rig.manager.stop()


@pytest.mark.asyncio
async def test_window_deadline_is_fixed_and_deferred_is_not_refused(
    tmp_path: Path, temp_db: HubDatabase
) -> None:
    """2.1.9: remaining_ms=0 never extends a window; `deferred` refuses nothing."""
    exe = tmp_path / "bin" / "gterm"
    installed = _install(exe, b"gterm 0.1.4")
    host = _Host(binary_sha256="a" * 64)
    host.reply = lambda attempt_id: host.accept(attempt_id, installed, remaining_ms=0)
    rig = _rig(tmp_path, TerminalManager(temp_db), host, exe)
    deaths = AsyncMock()

    with patch.object(rig.manager, "handle_host_death", new=deaths):
        await rig.manager.start()
        stuck = host.requests[0][1]
        host.in_progress(stuck, installed, "probing", 0)
        # The accept fixed the deadline one interval out; each later ping
        # reporting 0 ms left would push it a little further if it could.
        for _ in range(2):
            rig.clock.now += INTERVAL / 4
            await rig.ticks.run()
            deaths.assert_not_awaited()
        rig.clock.now += INTERVAL * 0.6
        await rig.ticks.run()
        deaths.assert_awaited_once()
        assert rig.manager.upgrade.window is None

        # The host still reports the finished attempt: no window reopens for it.
        await rig.ticks.run(2)
        assert rig.manager.upgrade.window is None
        assert await _attach_ready(rig.manager) is True
        assert len(host.requests) == 1

        host.finish(stuck, "aborted", installed)
        host.reply = lambda attempt_id: host.accept(attempt_id, installed)
        await rig.ticks.run()
        deferred = host.requests[-1][1]
        assert deferred != stuck
        host.finish(deferred, "deferred", installed)
        await rig.ticks.run()
        assert rig.manager.upgrade.window is None
        await rig.ticks.run()
        assert len(host.requests) == 3, "a deferred candidate is asked for again"
    deaths.assert_awaited_once()
    await rig.manager.stop()


def test_ping_decodes_upgrade_record() -> None:
    """The host's ping carries binary identity, generation, and the attempt record."""
    running = "d" * 64
    idle = parse_ping(
        {
            "ok": True,
            "host_epoch": "epoch-1",
            "version": "0.1.3",
            "binary_version": "0.1.3",
            "binary_sha256": running,
            "host_pid": HOST_PID,
            "generation": 2,
            "upgrade": {
                "attempt_id": None,
                "phase": "idle",
                "candidate_sha256": None,
                "remaining_ms": None,
                "last_outcome": {
                    "attempt_id": "a1",
                    "outcome": "rolled_back",
                    "candidate_sha256": "e" * 64,
                    "reason": "exec_failed",
                    "errno": 8,
                },
            },
        }
    )
    assert idle == PingResult(
        host_epoch="epoch-1",
        version="0.1.3",
        host_pid=HOST_PID,
        binary_sha256=running,
        generation=2,
        upgrade=UpgradeStatus(
            phase="idle",
            last_outcome=UpgradeOutcome(
                attempt_id="a1",
                outcome="rolled_back",
                candidate_sha256="e" * 64,
                reason="exec_failed",
            ),
        ),
    )
    probing = parse_ping(
        {
            "host_epoch": "epoch-1",
            "host_pid": HOST_PID,
            "binary_sha256": running,
            "generation": 2,
            "upgrade": {
                "attempt_id": "a2",
                "phase": "probing",
                "candidate_sha256": None,
                "remaining_ms": 14_250,
                "last_outcome": None,
            },
        }
    )
    assert probing.upgrade == UpgradeStatus(phase="probing", attempt_id="a2", remaining_ms=14_250)
    legacy = parse_ping({"host_epoch": "epoch-0", "version": "0.1.2", "host_pid": HOST_PID})
    assert legacy == PingResult(host_epoch="epoch-0", version="0.1.2", host_pid=HOST_PID)
