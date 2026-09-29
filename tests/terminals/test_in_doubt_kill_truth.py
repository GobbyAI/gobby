"""In-doubt spawn ownership and kill truth (placed-agent-launch plan 1.9)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from psycopg.types.json import Jsonb

from gobby.agents.lifecycle_reconciliation import LifecycleReconciliation
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from gobby.terminals.host_client import HostCommandError, HostUnavailableError
from gobby.terminals.host_protocol import HostListRow
from gobby.terminals.in_doubt import InDoubtRegistry, in_doubt_spawns
from gobby.terminals.native_runtime import NativeTerminalRuntime
from gobby.terminals.termination import (
    TerminalInDoubtError,
    TerminalKillUnprovenError,
    kill_terminal,
)
from gobby.utils.machine_id import require_machine_id
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000023"


@dataclass
class _HostClient:
    """Control client whose listing, reachability and connect-time epoch are scripted."""

    host_epoch: str = "epoch-now"
    available: bool = True
    adopt_epoch: str | None = None
    list_rows: list[HostListRow] = field(default_factory=list)
    kills: list[str] = field(default_factory=list)
    list_calls: int = 0

    async def ensure_connected(self) -> None:
        if not self.available:
            raise HostUnavailableError("gterm host unavailable")
        if self.adopt_epoch is not None:
            self.host_epoch = self.adopt_epoch

    async def list_terminals(self) -> list[HostListRow]:
        await self.ensure_connected()
        self.list_calls += 1
        return list(self.list_rows)

    async def kill(self, host_terminal_id: str, grace_ms: int = 50) -> None:
        del grace_ms
        await self.ensure_connected()
        self.kills.append(host_terminal_id)
        self.list_rows = [r for r in self.list_rows if r.host_terminal_id != host_terminal_id]


class _StickyRuntime(FakeRuntime):
    """A tmux runtime whose kill returns without removing the session."""

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        del terminal, grace_seconds


def _host_row(terminal: Terminal, host_terminal_id: str) -> HostListRow:
    return HostListRow(
        terminal_id=terminal.id,
        spawn_key=str(terminal.spawn_key),
        commit_state="committed",
        observer_bind="none",
        host_terminal_id=host_terminal_id,
    )


def _stale_native_row(*, state: str = "live") -> Terminal:
    row = make_memory_terminal(backend="native")
    row.spawn_key = row.id
    row.state = state
    if state == "pending":
        row.host_epoch = None
        row.locator = None
        row.locator_key = None
    else:
        row.host_epoch = "epoch-before-restart"
        row.locator = {"host_terminal_id": "ht-old"}
        row.locator_key = native_locator_key("epoch-before-restart", "ht-old")
    return row


class _ReapRecorder:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, group_alive: bool) -> None:
        self.reaped: list[dict[str, Any]] = []
        self.alive_checks = 0
        self._group_alive = group_alive
        monkeypatch.setattr("gobby.terminals.native_runtime.reap_recorded_process", self._reap)
        monkeypatch.setattr(
            "gobby.terminals.native_runtime.recorded_process_group_is_alive", self._alive
        )

    def _reap(self, process: Any, *, grace_seconds: float, now: float | None = None) -> None:
        del grace_seconds, now
        self.reaped.append(dict(process))

    def _alive(self, process: Any) -> bool:
        del process
        self.alive_checks += 1
        return self._group_alive


@pytest.fixture
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _orphaned_row(manager: TerminalManager, project_id: str) -> Terminal:
    tid = str(uuid.uuid4())
    pending = manager.create_pending(
        terminal_id=tid,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=tid,
        machine_id=require_machine_id(),
    )
    live = manager.promote_to_live(
        pending.id,
        locator={"host_terminal_id": f"ht-{tid[:8]}"},
        locator_key=native_locator_key("epoch-before-restart", f"ht-{tid[:8]}"),
        host_epoch="epoch-before-restart",
    )
    assert live is not None
    orphaned = manager.mark_orphaned(live.id)
    assert orphaned is not None
    return orphaned


@pytest.mark.asyncio
async def test_in_doubt_registry_claims_defers_and_releases() -> None:
    registry = InDoubtRegistry()
    terminal_id = str(uuid.uuid4())

    async def first() -> None:
        return None

    async def second() -> None:
        return None

    assert registry.holds(terminal_id) is False
    assert registry.defer(terminal_id, first) is False

    registry.claim(terminal_id)
    assert registry.holds(terminal_id) is True
    assert registry.defer(terminal_id, first) is True
    assert registry.defer(terminal_id, second) is True
    assert await asyncio.to_thread(registry.holds, terminal_id) is True

    assert registry.release(terminal_id) == [first, second]
    assert registry.holds(terminal_id) is False
    assert await asyncio.to_thread(registry.holds, terminal_id) is False
    assert registry.release(terminal_id) == []
    assert registry.defer(terminal_id, first) is False

    stop = asyncio.Event()
    observed: list[bool] = []

    def poll() -> None:
        while not stop.is_set():
            observed.append(registry.holds(terminal_id))

    poller = asyncio.create_task(asyncio.to_thread(poll))
    for _ in range(200):
        registry.claim(terminal_id)
        await asyncio.sleep(0)
        registry.release(terminal_id)
    stop.set()
    await poller
    assert observed
    assert set(observed) <= {True, False}
    assert registry.holds(terminal_id) is False


@pytest.mark.asyncio
async def test_kill_terminal_refuses_held_ids() -> None:
    terminal = make_memory_terminal(session_name="held-shell")
    terminals = MemoryTerminalStore(terminal)
    runtime = FakeRuntime()
    registry = runtime_registry(runtime)

    in_doubt_spawns.claim(terminal.id)
    try:
        with pytest.raises(TerminalInDoubtError):
            await kill_terminal(terminals, registry, terminal)
        assert runtime.killed == []
        assert terminal.state == "live"
    finally:
        in_doubt_spawns.release(terminal.id)

    exited = await kill_terminal(terminals, registry, terminal)

    assert exited is terminal
    assert terminal.state == "exited"
    assert runtime.killed == ["held-shell"]


@pytest.mark.asyncio
async def test_kill_terminal_requires_proven_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    # tmux: a terminate that leaves the session present is not a kill.
    tmux_row = make_memory_terminal(session_name="sticky-shell")
    sticky = _StickyRuntime()
    with pytest.raises(TerminalKillUnprovenError):
        await kill_terminal(MemoryTerminalStore(tmux_row), runtime_registry(sticky), tmux_row)
    assert tmux_row.state == "live"

    reaps = _ReapRecorder(monkeypatch, group_alive=False)

    # Stale epoch, the current host still lists it: kill the current host id.
    listed = _stale_native_row()
    client = _HostClient()
    client.list_rows = [_host_row(listed, "ht-current")]
    runtime = NativeTerminalRuntime(client)
    exited = await kill_terminal(MemoryTerminalStore(listed), runtime_registry(runtime), listed)
    assert exited is listed
    assert listed.state == "exited"
    assert client.kills == ["ht-current"]
    assert reaps.reaped == []

    # Stale epoch, absent from a strict listing: reap the recorded process (#22530).
    absent = _stale_native_row()
    absent.process = {"pgid": 4242, "start_time": 17.0}
    client = _HostClient()
    runtime = NativeTerminalRuntime(client)
    await kill_terminal(MemoryTerminalStore(absent), runtime_registry(runtime), absent)
    assert absent.state == "exited"
    assert client.kills == []
    assert reaps.reaped == [{"pgid": 4242, "start_time": 17.0}]

    # Host unreachable, usable process whose group is dead afterwards: settled.
    dead = _stale_native_row()
    dead.process = {"pgid": 5151, "start_time": 3.0}
    runtime = NativeTerminalRuntime(_HostClient(available=False))
    await kill_terminal(MemoryTerminalStore(dead), runtime_registry(runtime), dead)
    assert dead.state == "exited"
    assert reaps.reaped[-1] == {"pgid": 5151, "start_time": 3.0}

    # Host unreachable, group still alive: unsettled.
    alive_reaps = _ReapRecorder(monkeypatch, group_alive=True)
    alive = _stale_native_row()
    alive.process = {"pgid": 6161, "start_time": 4.0}
    runtime = NativeTerminalRuntime(_HostClient(available=False))
    with pytest.raises(HostUnavailableError):
        await kill_terminal(MemoryTerminalStore(alive), runtime_registry(runtime), alive)
    assert alive.state == "live"

    # Host unreachable, no usable process identity: never read as death.
    unusable: list[dict[str, Any] | None] = [
        None,
        {"host_terminal_id": "ht-old"},
        {"pgid": "6262", "start_time": 1.0},
        {"pgid": 0, "start_time": 1.0},
        {"pgid": -3, "start_time": 1.0},
        {"pgid": True, "start_time": 1.0},
    ]
    for process in unusable:
        row = _stale_native_row()
        row.process = process
        runtime = NativeTerminalRuntime(_HostClient(available=False))
        with pytest.raises(HostUnavailableError):
            await kill_terminal(MemoryTerminalStore(row), runtime_registry(runtime), row)
        assert row.state == "live"
    assert alive_reaps.reaped == [{"pgid": 6161, "start_time": 4.0}]
    assert alive_reaps.alive_checks == 1

    # A pending row carries no epoch and takes the same three branches.
    reaps = _ReapRecorder(monkeypatch, group_alive=False)
    pending_listed = _stale_native_row(state="pending")
    client = _HostClient()
    client.list_rows = [_host_row(pending_listed, "ht-pending")]
    await NativeTerminalRuntime(client).terminate(pending_listed, 0.05)
    assert client.kills == ["ht-pending"]

    pending_absent = _stale_native_row(state="pending")
    pending_absent.process = {"pgid": 7171, "start_time": 2.0}
    client = _HostClient()
    await NativeTerminalRuntime(client).terminate(pending_absent, 0.05)
    assert client.kills == []
    assert reaps.reaped == [{"pgid": 7171, "start_time": 2.0}]

    pending_unreachable = _stale_native_row(state="pending")
    with pytest.raises(HostUnavailableError):
        await NativeTerminalRuntime(_HostClient(available=False)).terminate(
            pending_unreachable, 0.05
        )

    # An unconnected client adopts its epoch before the compare: a current row is not stale.
    current = _stale_native_row()
    current.host_epoch = "epoch-1"
    current.locator = {"host_terminal_id": "ht-1"}
    client = _HostClient(host_epoch="", adopt_epoch="epoch-1")
    await NativeTerminalRuntime(client).terminate(current, 0.05)
    assert client.kills == ["ht-1"]
    assert client.list_calls == 0

    # A stale Terminal argument never decides: the row reread under settle_lock does.
    stored = _stale_native_row()
    stored.host_epoch = "epoch-now"
    stored.locator = {"host_terminal_id": "ht-stored"}
    stale_argument = replace(stored, host_epoch="epoch-gone", locator={"host_terminal_id": "x"})
    client = _HostClient()
    client.list_rows = [_host_row(stored, "ht-stored")]
    runtime = NativeTerminalRuntime(client)
    await kill_terminal(MemoryTerminalStore(stored), runtime_registry(runtime), stale_argument)
    assert client.kills == ["ht-stored"]
    assert stored.state == "exited"


def _current_native_row(process: dict[str, Any] | None) -> Terminal:
    row = _stale_native_row()
    row.host_epoch = "epoch-now"
    row.locator = {"host_terminal_id": "ht-now"}
    row.locator_key = native_locator_key("epoch-now", "ht-now")
    row.process = process
    return row


@pytest.mark.asyncio
async def test_native_kill_ack_needs_dead_recorded_group(monkeypatch: pytest.MonkeyPatch) -> None:
    # The host acks a kill before its child exits, so a usable recorded group
    # that outlives the grace window leaves the row unsettled.
    alive_reaps = _ReapRecorder(monkeypatch, group_alive=True)
    survivor = _current_native_row({"pgid": 8181, "start_time": 5.0})
    client = _HostClient()
    client.list_rows = [_host_row(survivor, "ht-now")]
    runtime = NativeTerminalRuntime(client)
    with pytest.raises(TerminalKillUnprovenError):
        await kill_terminal(
            MemoryTerminalStore(survivor), runtime_registry(runtime), survivor, grace_seconds=0.05
        )
    assert client.kills == ["ht-now"]
    assert survivor.state == "live"
    assert alive_reaps.alive_checks >= 1

    # The recorded group is dead after the ack: settled.
    dead_reaps = _ReapRecorder(monkeypatch, group_alive=False)
    dead = _current_native_row({"pgid": 8282, "start_time": 5.0})
    client = _HostClient()
    client.list_rows = [_host_row(dead, "ht-now")]
    runtime = NativeTerminalRuntime(client)
    exited = await kill_terminal(
        MemoryTerminalStore(dead), runtime_registry(runtime), dead, grace_seconds=0.05
    )
    assert exited is dead
    assert dead.state == "exited"
    assert dead_reaps.alive_checks == 1

    # No usable recorded group: the host ack plus its strict absence stay the proof.
    bare = _current_native_row({"host_terminal_id": "ht-now"})
    client = _HostClient()
    client.list_rows = [_host_row(bare, "ht-now")]
    runtime = NativeTerminalRuntime(client)
    await kill_terminal(
        MemoryTerminalStore(bare), runtime_registry(runtime), bare, grace_seconds=0.05
    )
    assert bare.state == "exited"
    assert dead_reaps.alive_checks == 1

    # The host itself reports the group alive after TERM, grace and KILL.
    refused = _current_native_row({"pgid": 8383, "start_time": 5.0})
    client = _UnprovenKillClient()
    client.list_rows = [_host_row(refused, "ht-now")]
    runtime = NativeTerminalRuntime(client)
    with pytest.raises(HostCommandError, match="kill_unproven"):
        await kill_terminal(
            MemoryTerminalStore(refused), runtime_registry(runtime), refused, grace_seconds=0.05
        )
    assert refused.state == "live"
    assert dead_reaps.alive_checks == 1


class _UnprovenKillClient(_HostClient):
    async def kill(self, host_terminal_id: str, grace_ms: int = 50) -> None:
        del host_terminal_id, grace_ms
        raise HostCommandError("kill_unproven")


@pytest.mark.usefixtures("_local_machine_identity")
def test_record_orphan_identity_cas(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = TerminalManager(temp_db)
    orphaned = _orphaned_row(manager, sample_project["id"])
    identity: dict[str, Any] = {
        "locator": {"host_terminal_id": "ht-recovered"},
        "locator_key": native_locator_key("epoch-now", "ht-recovered"),
        "host_epoch": "epoch-now",
        "process": {"host_terminal_id": "ht-recovered", "pgid": 8181, "start_time": 9.5},
    }

    recorded = manager.record_orphan_identity(
        orphaned.id,
        attempt_generation=orphaned.attempt_generation,
        attempt_started_at=orphaned.attempt_started_at,
        **identity,
    )

    assert recorded is not None
    assert recorded.state == "orphaned"
    assert recorded.locator == {"host_terminal_id": "ht-recovered"}
    assert recorded.locator_key == native_locator_key("epoch-now", "ht-recovered")
    assert recorded.host_epoch == "epoch-now"
    assert recorded.process is not None
    assert recorded.process["pgid"] == 8181
    assert recorded.process["host_terminal_id"] == "ht-recovered"

    # A later host that lists no pgid must not inherit the earlier host's
    # group, while spawn metadata such as the shell survives.
    temp_db.execute(
        "UPDATE terminals SET process = process || %s WHERE id = %s",
        (Jsonb({"shell": "zsh"}), orphaned.id),
    )
    rerecorded = manager.record_orphan_identity(
        orphaned.id,
        attempt_generation=orphaned.attempt_generation,
        attempt_started_at=orphaned.attempt_started_at,
        locator={"host_terminal_id": "ht-next"},
        locator_key=native_locator_key("epoch-next", "ht-next"),
        host_epoch="epoch-next",
        process={"host_terminal_id": "ht-next"},
    )
    assert rerecorded is not None
    assert rerecorded.process == {"host_terminal_id": "ht-next", "shell": "zsh"}

    pending = manager.create_pending(
        terminal_id=str(uuid.uuid4()),
        project_id=sample_project["id"],
        backend="native",
        ownership="gobby",
        spawn_key=str(uuid.uuid4()),
        machine_id=require_machine_id(),
    )
    live = _orphaned_row(manager, sample_project["id"])
    exited = manager.mark_exited(live.id)
    assert exited is not None
    for other in (pending, exited):
        before = manager.get(other.id)
        assert (
            manager.record_orphan_identity(
                other.id,
                attempt_generation=other.attempt_generation,
                attempt_started_at=other.attempt_started_at,
                **identity,
            )
            is None
        )
        assert manager.get(other.id) == before


@pytest.mark.asyncio
async def test_probe_is_strict_and_reaper_honors_claims() -> None:
    row = _stale_native_row()
    client = _HostClient()
    client.list_rows = [_host_row(row, "ht-listed")]
    runtime = NativeTerminalRuntime(client)

    assert await runtime.find_host_terminal(row.id, str(row.spawn_key)) == "ht-listed"
    assert await runtime.find_host_terminal(row.id, "other-spawn-key") is None
    assert await runtime.find_host_terminal(str(uuid.uuid4()), str(row.spawn_key)) is None
    client.available = False
    with pytest.raises(HostUnavailableError):
        await runtime.find_host_terminal(row.id, str(row.spawn_key))

    old = datetime.now(UTC) - timedelta(seconds=120)
    held = _stale_native_row(state="pending")
    held.attempt_started_at = old
    free = _stale_native_row(state="pending")
    free.attempt_started_at = old
    store = MemoryTerminalStore(held)
    store.rows[free.id] = free
    reconciliation = LifecycleReconciliation(
        agent_run_manager=MagicMock(),
        db=MagicMock(),
        cleanup_handler=MagicMock(),
        run_db=AsyncMock(),
        terminal_manager=store,
        # The strict listing of a reachable host that lists neither row.
        runtime_registry=runtime_registry(NativeTerminalRuntime(_HostClient())),
        spawn_in_doubt_seconds=30.0,
    )

    in_doubt_spawns.claim(held.id)
    try:
        reaped = await reconciliation.reap_stale_pending()
    finally:
        in_doubt_spawns.release(held.id)

    assert reaped == 1
    assert free.state == "exited"
    assert held.state == "pending"


@pytest.mark.parametrize("listed", [True, False], ids=["host-listed", "host-miss"])
async def test_stale_native_kill_needs_dead_recorded_group(
    monkeypatch: pytest.MonkeyPatch, listed: bool
) -> None:
    # A stale-epoch kill proves nothing while the recorded group is alive,
    # whether the current host listed the row or the recorded group was reaped.
    reaps = _ReapRecorder(monkeypatch, group_alive=True)
    row = _stale_native_row()
    row.process = {"pgid": 8383, "start_time": 5.0}
    client = _HostClient()
    if listed:
        client.list_rows = [_host_row(row, "ht-listed")]
    runtime = NativeTerminalRuntime(client)

    with pytest.raises(TerminalKillUnprovenError):
        await runtime.terminate(row, 0.05)

    assert client.kills == (["ht-listed"] if listed else [])
    assert reaps.alive_checks >= 1


def _deny_signals(pgid: int, sig: int) -> None:
    raise PermissionError(pgid, sig)


def _no_such_group(pgid: int, sig: int) -> None:
    raise ProcessLookupError(pgid, sig)


@pytest.mark.parametrize("group", ["permission-denied", "dead"])
async def test_reaper_needs_dead_group_for_hostless_pending_row(
    monkeypatch: pytest.MonkeyPatch, group: str
) -> None:
    signal = _deny_signals if group == "permission-denied" else _no_such_group
    monkeypatch.setattr("gobby.terminals.host_reap.os.killpg", signal)
    monkeypatch.setattr("gobby.terminals.host_reap.os.kill", signal)
    row = _stale_native_row(state="pending")
    row.process = {"pgid": 9191}
    row.attempt_started_at = datetime.now(UTC) - timedelta(seconds=120)
    reconciliation = LifecycleReconciliation(
        agent_run_manager=MagicMock(),
        db=MagicMock(),
        cleanup_handler=MagicMock(),
        run_db=AsyncMock(),
        terminal_manager=MemoryTerminalStore(row),
        # A reachable host that lists nothing: only the recorded group is left.
        runtime_registry=runtime_registry(NativeTerminalRuntime(_HostClient())),
        spawn_in_doubt_seconds=30.0,
    )

    reaped = await reconciliation.reap_stale_pending()

    if group == "permission-denied":
        assert reaped == 0
        assert row.state == "pending"
    else:
        assert reaped == 1
        assert row.state == "exited"
