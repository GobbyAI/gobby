"""Restart reconcile recovers an orphan's current host identity (plan 1.9)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from psycopg.types.json import Jsonb

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from gobby.terminals.host_client import HostUnavailableError
from gobby.terminals.host_protocol import HostListRow
from gobby.terminals.host_reconcile import reconcile_host_inventory
from gobby.terminals.in_doubt import in_doubt_spawns
from gobby.terminals.native_runtime import NativeTerminalRuntime
from gobby.terminals.termination import kill_terminal
from gobby.utils.machine_id import require_machine_id
from tests.terminals.fakes import runtime_registry

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000024"
CURRENT_EPOCH = "epoch-after-restart"


@dataclass
class _HostClient:
    host_epoch: str = CURRENT_EPOCH
    list_rows: list[HostListRow] = field(default_factory=list)
    kills: list[str] = field(default_factory=list)

    async def ensure_connected(self) -> None:
        return None

    async def list_terminals(self) -> list[HostListRow]:
        return list(self.list_rows)

    async def kill(self, host_terminal_id: str, grace_ms: int = 50) -> None:
        del grace_ms
        self.kills.append(host_terminal_id)
        self.list_rows = [r for r in self.list_rows if r.host_terminal_id != host_terminal_id]


class _ChangingManager(TerminalManager):
    """Applies one scripted change to a row the first time reconcile rereads it."""

    def __init__(self, db: HubDatabase) -> None:
        super().__init__(db)
        self.changes: dict[str, Callable[[], object]] = {}

    def get(self, terminal_id: str) -> Terminal | None:
        change = self.changes.pop(terminal_id, None)
        if change is not None:
            change()
        return super().get(terminal_id)


class _LockWatchingManager(TerminalManager):
    """Signals when a caller starts waiting on a watched terminal's settle lock."""

    def __init__(self, db: HubDatabase) -> None:
        super().__init__(db)
        self.requested: dict[str, asyncio.Event] = {}

    @asynccontextmanager
    async def settle_lock(self, terminal_id: str) -> AsyncIterator[None]:
        event = self.requested.get(terminal_id)
        if event is not None:
            event.set()
        async with super().settle_lock(terminal_id):
            yield


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
    stale_host_id = f"ht-stale-{tid[:8]}"
    live = manager.promote_to_live(
        pending.id,
        locator={"host_terminal_id": stale_host_id},
        locator_key=native_locator_key("epoch-before-restart", stale_host_id),
        host_epoch="epoch-before-restart",
    )
    assert live is not None
    orphaned = manager.mark_orphaned(live.id)
    assert orphaned is not None
    return orphaned


def _listed(terminal: Terminal, host_terminal_id: str, **process: Any) -> HostListRow:
    return HostListRow(
        terminal_id=terminal.id,
        spawn_key=str(terminal.spawn_key),
        commit_state="committed",
        observer_bind="none",
        host_terminal_id=host_terminal_id,
        **process,
    )


@pytest.mark.usefixtures("_local_machine_identity")
@pytest.mark.asyncio
async def test_reconcile_recovers_orphan_identity(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _ChangingManager(temp_db)
    project_id = sample_project["id"]
    recover = _orphaned_row(manager, project_id)
    held = _orphaned_row(manager, project_id)
    moved = _orphaned_row(manager, project_id)
    rebumped = _orphaned_row(manager, project_id)
    host_rows = [
        _listed(recover, "ht-recover", pgid=9191, start_time=4.0),
        _listed(held, "ht-held"),
        _listed(moved, "ht-moved"),
        _listed(rebumped, "ht-rebumped"),
    ]
    manager.changes[moved.id] = lambda: manager.mark_exited(moved.id)
    manager.changes[rebumped.id] = lambda: temp_db.execute(
        "UPDATE terminals SET attempt_generation = attempt_generation + 1 WHERE id = %s",
        (rebumped.id,),
    )
    host_kills: list[str] = []

    async def record_kill(host_terminal_id: str) -> None:
        host_kills.append(host_terminal_id)

    in_doubt_spawns.claim(held.id)
    try:
        await reconcile_host_inventory(
            terminal_manager=manager,
            machine_id=require_machine_id(),
            host_epoch=CURRENT_EPOCH,
            host_rows=host_rows,
            spawn_in_doubt_seconds=30.0,
            run_manager=None,
            kill=record_kill,
        )
    finally:
        in_doubt_spawns.release(held.id)

    assert host_kills == []
    recovered = manager.get(recover.id)
    assert recovered is not None
    assert recovered.state == "orphaned"
    assert recovered.locator == {"host_terminal_id": "ht-recover"}
    assert recovered.locator_key == native_locator_key(CURRENT_EPOCH, "ht-recover")
    assert recovered.host_epoch == CURRENT_EPOCH
    assert recovered.process is not None
    assert recovered.process["pgid"] == 9191
    assert recovered.process["start_time"] == 4.0
    for untouched in (held, moved, rebumped):
        row = manager.get(untouched.id)
        assert row is not None
        assert row.locator == untouched.locator
        assert row.host_epoch == "epoch-before-restart"
    moved_row = manager.get(moved.id)
    assert moved_row is not None
    assert moved_row.state == "exited"

    # The recovered pgid is fictional; its group must not be probed on this machine.
    monkeypatch.setattr(
        "gobby.terminals.native_runtime.recorded_process_group_is_alive", lambda _process: False
    )
    client = _HostClient(list_rows=[host_rows[0]])
    runtime = NativeTerminalRuntime(client)
    exited = await kill_terminal(manager, runtime_registry(runtime), recovered)
    assert client.kills == ["ht-recover"]
    assert exited is not None
    assert exited.state == "exited"

    other_generation = held.attempt_generation + 1
    assert (
        manager.mark_exited_attempt(
            held.id,
            attempt_generation=other_generation,
            attempt_started_at=held.attempt_started_at,
        )
        is None
    )
    assert (
        manager.record_orphan_identity(
            held.id,
            attempt_generation=other_generation,
            attempt_started_at=held.attempt_started_at,
            locator={"host_terminal_id": "ht-held"},
            locator_key=native_locator_key(CURRENT_EPOCH, "ht-held"),
            host_epoch=CURRENT_EPOCH,
            process=None,
        )
        is None
    )
    unchanged = manager.get(held.id)
    assert unchanged is not None
    assert unchanged.state == "orphaned"
    assert unchanged.host_epoch == "epoch-before-restart"
    settled = manager.mark_exited_attempt(
        held.id,
        attempt_generation=held.attempt_generation,
        attempt_started_at=held.attempt_started_at,
    )
    assert settled is not None
    assert settled.state == "exited"


class _UnreachableHostClient(_HostClient):
    async def list_terminals(self) -> list[HostListRow]:
        raise HostUnavailableError("gterm host socket unavailable")


def _current_epoch_orphan(manager: TerminalManager, project_id: str) -> Terminal:
    orphaned = _orphaned_row(manager, project_id)
    host_id = f"ht-now-{orphaned.id[:8]}"
    stamped = manager.record_orphan_identity(
        orphaned.id,
        attempt_generation=orphaned.attempt_generation,
        attempt_started_at=orphaned.attempt_started_at,
        locator={"host_terminal_id": host_id},
        locator_key=native_locator_key(CURRENT_EPOCH, host_id),
        host_epoch=CURRENT_EPOCH,
        process={"host_terminal_id": host_id},
    )
    assert stamped is not None
    return stamped


@pytest.mark.usefixtures("_local_machine_identity")
@pytest.mark.asyncio
async def test_reconcile_settles_current_epoch_orphan_on_host_absence(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _ChangingManager(temp_db)
    project_id = sample_project["id"]
    absent = _current_epoch_orphan(manager, project_id)
    unreachable = NativeTerminalRuntime(
        _UnreachableHostClient(),
        terminal_manager=manager,
        machine_id=require_machine_id(),
    )
    with pytest.raises(HostUnavailableError):
        await unreachable.reconnect()
    unproven = manager.get(absent.id)
    assert unproven is not None
    assert unproven.state == "orphaned"
    listed = _current_epoch_orphan(manager, project_id)
    held = _current_epoch_orphan(manager, project_id)
    rebumped = _current_epoch_orphan(manager, project_id)
    manager.changes[rebumped.id] = lambda: temp_db.execute(
        "UPDATE terminals SET attempt_generation = attempt_generation + 1 WHERE id = %s",
        (rebumped.id,),
    )
    host_kills: list[str] = []

    async def record_kill(host_terminal_id: str) -> None:
        host_kills.append(host_terminal_id)

    in_doubt_spawns.claim(held.id)
    try:
        await reconcile_host_inventory(
            terminal_manager=manager,
            machine_id=require_machine_id(),
            host_epoch=CURRENT_EPOCH,
            host_rows=[_listed(listed, "ht-listed")],
            spawn_in_doubt_seconds=30.0,
            run_manager=None,
            kill=record_kill,
        )
    finally:
        in_doubt_spawns.release(held.id)

    assert host_kills == []
    settled = manager.get(absent.id)
    assert settled is not None
    assert settled.state == "exited"
    for kept in (listed, held, rebumped):
        row = manager.get(kept.id)
        assert row is not None
        assert row.state == "orphaned"


def _current_epoch_live(manager: TerminalManager, project_id: str, pgid: int) -> Terminal:
    tid = str(uuid.uuid4())
    pending = manager.create_pending(
        terminal_id=tid,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=tid,
        machine_id=require_machine_id(),
    )
    host_id = f"ht-live-{tid[:8]}"
    live = manager.promote_to_live(
        pending.id,
        locator={"host_terminal_id": host_id},
        locator_key=native_locator_key(CURRENT_EPOCH, host_id),
        host_epoch=CURRENT_EPOCH,
    )
    assert live is not None
    manager.db.execute(
        "UPDATE terminals SET process = %s WHERE id = %s",
        (Jsonb({"host_terminal_id": host_id, "pgid": pgid, "start_time": 1.0}), live.id),
    )
    return live


@pytest.mark.usefixtures("_local_machine_identity")
@pytest.mark.asyncio
async def test_reconcile_exits_absent_live_row_only_once_its_group_is_dead(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _LockWatchingManager(temp_db)
    project_id = sample_project["id"]
    alive = _current_epoch_live(manager, project_id, pgid=7001)
    dead = _current_epoch_live(manager, project_id, pgid=7002)
    locked = _current_epoch_live(manager, project_id, pgid=7003)
    # An old host drops a slot while its kill is still proving the group
    # gone, so absence alone must not settle a group that is still alive.
    monkeypatch.setattr(
        "gobby.terminals.host_reconcile.recorded_process_group_is_alive",
        lambda process: process is not None and process.get("pgid") == 7001,
    )

    async def record_kill(host_terminal_id: str) -> None:
        raise AssertionError(f"reconcile killed {host_terminal_id}")

    async with manager.settle_lock(locked.id):
        requested = manager.requested[locked.id] = asyncio.Event()
        reconcile = asyncio.create_task(
            reconcile_host_inventory(
                terminal_manager=manager,
                machine_id=require_machine_id(),
                host_epoch=CURRENT_EPOCH,
                host_rows=[],
                spawn_in_doubt_seconds=30.0,
                run_manager=None,
                kill=record_kill,
            )
        )
        # Reconcile must queue behind the kill's lock instead of settling.
        await asyncio.wait_for(requested.wait(), timeout=10)
        waiting = manager.get(locked.id)
        assert waiting is not None
        assert waiting.state == "live", "reconcile settled a row an in-flight kill holds"
    await reconcile

    states = {
        row.id: row.state for row in (manager.get(t.id) for t in (alive, dead, locked)) if row
    }
    assert states == {alive.id: "live", dead.id: "exited", locked.id: "exited"}
