"""Unproven-kill settlement CAS (placed-agent-launch plan 1.6.1)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminal_settlement import OrphanIdentity
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.unit


def _pending(manager: TerminalManager, project_id: str) -> Terminal:
    tid = str(uuid.uuid4())
    return manager.create_pending(
        terminal_id=tid,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=tid,
        machine_id=require_machine_id(),
    )


def _live(manager: TerminalManager, project_id: str) -> Terminal:
    pending = _pending(manager, project_id)
    host_id = f"ht-{pending.id[:8]}"
    live = manager.promote_to_live(
        pending.id,
        locator={"host_terminal_id": host_id},
        locator_key=native_locator_key("epoch-live", host_id),
        host_epoch="epoch-live",
    )
    assert live is not None
    return live


def _kill_failed(
    manager: TerminalManager,
    row: Terminal,
    *,
    identity: OrphanIdentity | None = None,
    generation_offset: int = 0,
) -> Terminal | None:
    return manager.mark_kill_failed(
        row.id,
        attempt_generation=row.attempt_generation + generation_offset,
        attempt_started_at=row.attempt_started_at,
        identity=identity,
    )


def test_mark_kill_failed_cas(temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
    manager = TerminalManager(temp_db)
    project_id = sample_project["id"]

    live = _live(manager, project_id)
    orphaned = _kill_failed(manager, live)
    assert orphaned is not None
    assert orphaned.state == "orphaned"
    assert orphaned.locator_key == live.locator_key
    assert orphaned.host_epoch == "epoch-live"

    native_pending = _pending(manager, project_id)
    native_identity = OrphanIdentity(
        locator={"host_terminal_id": "ht-unproven"},
        locator_key=native_locator_key("epoch-now", "ht-unproven"),
        host_epoch="epoch-now",
        process={"host_terminal_id": "ht-unproven", "pgid": 4242, "start_time": 1.5},
    )
    native_orphan = _kill_failed(manager, native_pending, identity=native_identity)
    assert native_orphan is not None
    assert native_orphan.state == "orphaned"
    assert native_orphan.locator == {"host_terminal_id": "ht-unproven"}
    assert native_orphan.locator_key == native_identity.locator_key
    assert native_orphan.host_epoch == "epoch-now"
    assert native_orphan.process is not None
    assert native_orphan.process["pgid"] == 4242

    # Without an identity a pending row cannot become an orphan: it keeps the
    # seat for a strict reaper or reconcile.
    bare_pending = _pending(manager, project_id)
    assert _kill_failed(manager, bare_pending) is None
    assert manager.get(bare_pending.id) == bare_pending

    exited = manager.mark_exited(_live(manager, project_id).id)
    assert exited is not None
    already_orphaned = manager.get(orphaned.id)
    assert already_orphaned is not None
    moved_on_live = _live(manager, project_id)
    moved_on_pending = _pending(manager, project_id)
    unchanged: list[tuple[Terminal, int]] = [
        (exited, 0),
        (already_orphaned, 0),
        (moved_on_live, 1),
        (moved_on_pending, 1),
    ]
    for row, offset in unchanged:
        assert (
            _kill_failed(manager, row, identity=native_identity, generation_offset=offset) is None
        )
        assert manager.get(row.id) == row

    stale_started = manager.get(moved_on_live.id)
    assert stale_started is not None
    assert (
        manager.mark_kill_failed(
            stale_started.id,
            attempt_generation=stale_started.attempt_generation,
            attempt_started_at=stale_started.attempt_started_at - timedelta(seconds=1),
        )
        is None
    )
    assert manager.get(stale_started.id) == stale_started
