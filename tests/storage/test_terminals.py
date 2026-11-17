"""Terminals table and TerminalManager storage contract (plan 2.1)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from psycopg.errors import CheckViolation, NotNullViolation, UniqueViolation

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminal_locators import native_locator_key as split_native_locator_key
from gobby.storage.terminal_settlement import OrphanIdentity
from gobby.storage.terminals import (
    ALLOWED_EDGES,
    TERMINAL_STATES,
    TITLE_MAX_BYTES,
    UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES,
    UNRESOLVED_WRITE_MAX_ENTRIES,
    UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES,
    HostEpochMismatchError,
    IllegalTerminalTransitionError,
    Terminal,
    TerminalManager,
    UnresolvedWriteCapacityError,
    native_locator_key,
    truncate_title,
)
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_USER_ID
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
_EPOCH = "31000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _native_locator(host_terminal_id: str = "host-1") -> dict[str, object]:
    return {"host_terminal_id": host_terminal_id}


def _native_key(locator: dict[str, object]) -> str:
    return native_locator_key(_EPOCH, str(locator["host_terminal_id"]))


def _manager(temp_db: HubDatabase) -> TerminalManager:
    return TerminalManager(temp_db)


def test_native_identity_round_trips_and_stays_unique(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])
    locator = _native_locator("round-trip-host")
    key = split_native_locator_key(_EPOCH, "round-trip-host")
    assert native_locator_key(_EPOCH, "round-trip-host") == key
    live = manager.promote_to_live(pending.id, locator=locator, locator_key=key, host_epoch=_EPOCH)
    assert live is not None
    stored = manager.get(live.id)
    assert stored is not None
    assert stored.locator == locator
    assert stored.locator_key == key
    attached = manager.attach_locator(live.id, live_host_epoch=_EPOCH, socket_dir=tmp_path)
    assert attached.backend == "native"
    assert attached.host_terminal_id == "round-trip-host"
    duplicate = _create_pending(manager, sample_project["id"])
    with pytest.raises(UniqueViolation):
        manager.promote_to_live(duplicate.id, locator=locator, locator_key=key, host_epoch=_EPOCH)
    with pytest.raises(UniqueViolation):
        _create_pending(manager, sample_project["id"], spawn_key=pending.spawn_key)


def test_terminal_storage_and_model_share_native_row_shape(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    row = _create_pending(_manager(temp_db), sample_project["id"])
    stored = temp_db.fetchone("SELECT to_jsonb(t) AS row FROM terminals t WHERE id = %s", (row.id,))
    assert stored is not None
    payload = stored["row"]
    columns = set(json.loads(payload) if isinstance(payload, str) else payload)
    assert {"id", "backend", "ownership", "host_epoch", "locator"} <= columns
    assert columns.isdisjoint({"session_name", "window_id"})
    assert set(Terminal.__dataclass_fields__).isdisjoint({"session_name", "window_id"})


def _create_pending(
    manager: TerminalManager,
    project_id: str,
    *,
    backend: str = "native",
    terminal_id: str | None = None,
    spawn_key: str | None = None,
) -> Terminal:
    tid = terminal_id or str(uuid.uuid4())
    key = spawn_key
    if key is None:
        key = tid
    return manager.create_pending(
        terminal_id=tid,
        project_id=project_id,
        backend=backend,
        ownership="gobby",
        spawn_key=key,
        machine_id=require_machine_id(),
    )


def test_get_many_fetches_any_number_of_terminals_in_one_query(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    rows = [_create_pending(manager, sample_project["id"]) for _ in range(3)]
    with patch.object(temp_db, "fetchall", wraps=temp_db.fetchall) as fetchall:
        found = manager.get_many([row.id for row in rows] + [rows[0].id])
    assert set(found) == {row.id for row in rows}
    assert fetchall.call_count == 1
    assert "id = ANY(%s)" in fetchall.call_args.args[0]
    assert len(fetchall.call_args.args[1][0]) == 3


def test_reads_decode_each_json_column_once_without_redumping(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    # The pool's jsonb loader decoded and re-dumped locator, process and unresolved
    # writes, and Terminal.from_row decoded that text again, on every read (#23359).
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"], backend="native")
    process = {"host_terminal_id": "ht-decode", "pgid": 4242, "start_time": 1_700_000_000}
    manager.record_process(
        pending.id,
        process,
        attempt_generation=pending.attempt_generation,
        attempt_started_at=pending.attempt_started_at,
    )
    epoch = str(uuid.uuid4())
    live = manager.promote_to_live(
        pending.id,
        locator={"host_terminal_id": "ht-decode"},
        locator_key=native_locator_key(epoch, "ht-decode"),
        host_epoch=epoch,
    )
    assert live is not None
    writes = {"ws:a": {"origin": "ws", "daemon_epoch": "e1"}}
    temp_db.execute(
        "UPDATE terminals SET unresolved_writes = %s::jsonb WHERE id = %s",
        (json.dumps(writes), live.id),
    )
    stale = _create_pending(manager, sample_project["id"], backend="native")
    manager.record_process(
        stale.id,
        process,
        attempt_generation=stale.attempt_generation,
        attempt_started_at=stale.attempt_started_at,
    )
    temp_db.execute(
        "UPDATE terminals SET unresolved_writes = %s::jsonb WHERE id = %s",
        (json.dumps(writes), stale.id),
    )

    with (
        patch("json.loads", wraps=json.loads) as loads,
        patch("json.dumps", wraps=json.dumps) as dumps,
    ):
        reads = [
            manager.get(live.id),
            manager.get_many([live.id])[live.id],
            *manager.list_live_by_epoch(epoch),
            *manager.list_stale_pending(0),
        ]

    assert dumps.call_count == 0
    live_reads = [read for read in reads if read is not None and read.id == live.id]
    stale_reads = [read for read in reads if read is not None and read.id == stale.id]
    assert len(live_reads) == 3
    assert len(stale_reads) == 1
    # Pending rows cannot store a locator. The reaper read still decodes the
    # JSON columns that row is allowed to hold.
    assert loads.call_count == 3 * len(live_reads) + 2 * len(stale_reads)
    for read in live_reads:
        assert read.locator == {"host_terminal_id": "ht-decode"}
        assert read.process == process
        assert read.unresolved_writes == writes
    assert stale_reads[0].locator is None
    assert stale_reads[0].process == process
    assert stale_reads[0].unresolved_writes == writes


def test_latest_for_session_includes_orphaned_terminal_evidence(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    manager = _manager(temp_db)
    session_id = session_manager.register(
        external_id="orphaned-evidence-session",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    ).id
    terminal_id = str(uuid.uuid4())
    pending = manager.create_pending(
        terminal_id=terminal_id,
        project_id=sample_project["id"],
        backend="native",
        ownership="gobby",
        spawn_key=terminal_id,
        session_id=session_id,
    )
    host_terminal_id = str(uuid.uuid4())
    epoch = str(uuid.uuid4())
    live = manager.promote_to_live(
        pending.id,
        locator={"host_terminal_id": host_terminal_id},
        locator_key=native_locator_key(epoch, host_terminal_id),
        host_epoch=epoch,
    )
    assert live is not None
    orphaned = manager.mark_orphaned(live.id)
    assert orphaned is not None

    assert manager.get_live_for_session(session_id) is None
    rows = manager.list_for_session(session_id)
    assert len(rows) == 1
    assert (rows[0].id, rows[0].session_id, rows[0].state) == (
        terminal_id,
        session_id,
        "orphaned",
    )


def test_failed_spawn_leaves_reapable_pending_row(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])

    stale = manager.list_stale_pending(max_age_seconds=0)
    assert pending.id in {row.id for row in stale}
    assert pending.spawn_key == pending.id
    assert pending.locator is None
    assert pending.backend == "native"

    native = _create_pending(manager, sample_project["id"], backend="native")
    assert native.spawn_key == native.id
    assert native.id in {row.id for row in manager.list_stale_pending(max_age_seconds=0)}


def test_exit_without_effect_and_locator_is_never_cleared(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])
    manager.fail_pending(pending.id)
    exited = manager.get(pending.id)
    assert exited is not None
    assert exited.state == "exited"
    assert exited.locator is None
    assert exited.locator_key is None

    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id
            ) VALUES (%s, 'native', 'gobby', 'live', %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                f"gobby-{uuid.uuid4()}",
                require_machine_id(),
                sample_project["id"],
            ),
        )
    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id
            ) VALUES (%s, 'native', 'gobby', 'orphaned', %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                f"gobby-{uuid.uuid4()}",
                require_machine_id(),
                sample_project["id"],
            ),
        )

    live = _create_pending(manager, sample_project["id"])
    locator = _native_locator(host_terminal_id="%99")
    manager.promote_to_live(
        live.id, locator=locator, locator_key=_native_key(locator), host_epoch=_EPOCH
    )
    manager.mark_exited(live.id)
    after = manager.get(live.id)
    assert after is not None
    assert after.locator == locator
    assert after.locator_key == _native_key(locator)


def test_row_and_edge_constraints_reject_illegal_variants(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    locator = _native_locator(host_terminal_id="%edge")
    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id,
                locator, locator_key
            ) VALUES (%s, 'native', 'gobby', 'pending', %s, %s, %s, %s::jsonb, %s)
            """,
            (
                str(uuid.uuid4()),
                f"gobby-{uuid.uuid4()}",
                require_machine_id(),
                sample_project["id"],
                json.dumps(locator),
                _native_key(locator),
            ),
        )
    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id, locator
            ) VALUES (%s, 'native', 'gobby', 'exited', %s, %s, %s, %s::jsonb)
            """,
            (
                str(uuid.uuid4()),
                f"gobby-{uuid.uuid4()}",
                require_machine_id(),
                sample_project["id"],
                json.dumps(locator),
            ),
        )
    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, machine_id, project_id, locator, locator_key
            ) VALUES (%s, 'native', 'external', 'pending', %s, %s, %s::jsonb, %s)
            """,
            (
                str(uuid.uuid4()),
                require_machine_id(),
                sample_project["id"],
                json.dumps(locator),
                _native_key(locator) + "-ext",
            ),
        )
    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id, host_epoch
            ) VALUES (%s, 'native', 'gobby', 'pending', %s, %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                f"gobby-{uuid.uuid4()}",
                require_machine_id(),
                sample_project["id"],
                str(uuid.uuid4()),
            ),
        )
    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id,
                locator, locator_key
            ) VALUES (%s, 'native', 'gobby', 'live', %s, %s, %s, %s::jsonb, %s)
            """,
            (
                str(uuid.uuid4()),
                f"gobby-{uuid.uuid4()}",
                require_machine_id(),
                sample_project["id"],
                json.dumps({"host_terminal_id": "t1"}),
                native_locator_key("epoch", "t1"),
            ),
        )

    for from_state in TERMINAL_STATES:
        for to_state in TERMINAL_STATES:
            _assert_edge(manager, sample_project["id"], from_state, to_state)


def _assert_edge(
    manager: TerminalManager,
    project_id: str,
    from_state: str,
    to_state: str,
) -> None:
    row = _row_in_state(manager, project_id, from_state)
    before = manager.db.fetchone("SELECT * FROM terminals WHERE id = %s", (row.id,))
    assert before is not None
    edge = (from_state, to_state)
    if edge in ALLOWED_EDGES:
        _apply_allowed_edge(manager, row, from_state, to_state)
        after = manager.get(row.id)
        assert after is not None
        assert after.state == to_state
        return
    with pytest.raises(IllegalTerminalTransitionError):
        manager.transition_for_test(row.id, from_state, to_state)
    after_row = manager.db.fetchone("SELECT * FROM terminals WHERE id = %s", (row.id,))
    assert after_row is not None
    assert dict(after_row) == dict(before)


def _row_in_state(manager: TerminalManager, project_id: str, state: str) -> Terminal:
    if state == "pending":
        return _create_pending(manager, project_id)
    if state == "live":
        row = _create_pending(manager, project_id)
        locator = _native_locator(host_terminal_id=f"%{uuid.uuid4().hex[:6]}")
        promoted = manager.promote_to_live(
            row.id, locator=locator, locator_key=_native_key(locator), host_epoch=_EPOCH
        )
        assert promoted is not None
        return promoted
    if state == "orphaned":
        row = _create_pending(manager, project_id, backend="native")
        host_terminal_id = str(uuid.uuid4())
        epoch = str(uuid.uuid4())
        promoted = manager.promote_to_live(
            row.id,
            locator={"host_terminal_id": host_terminal_id},
            locator_key=native_locator_key(epoch, host_terminal_id),
            host_epoch=epoch,
        )
        assert promoted is not None
        orphaned = manager.mark_orphaned(row.id)
        assert orphaned is not None
        return orphaned
    row = _create_pending(manager, project_id)
    exited = manager.fail_pending(row.id)
    assert exited is not None
    return exited


def _apply_allowed_edge(
    manager: TerminalManager,
    row: Terminal,
    from_state: str,
    to_state: str,
) -> None:
    if (from_state, to_state) == ("pending", "live"):
        locator = _native_locator(host_terminal_id=f"%{uuid.uuid4().hex[:6]}")
        assert manager.promote_to_live(
            row.id, locator=locator, locator_key=_native_key(locator), host_epoch=_EPOCH
        )
        return
    if (from_state, to_state) == ("pending", "exited"):
        assert manager.fail_pending(row.id)
        return
    if (from_state, to_state) == ("pending", "orphaned"):
        locator = _native_locator(host_terminal_id=f"%{uuid.uuid4().hex[:6]}")
        assert manager.mark_kill_failed(
            row.id,
            attempt_generation=row.attempt_generation,
            attempt_started_at=row.attempt_started_at,
            identity=OrphanIdentity(
                locator=locator, locator_key=_native_key(locator), host_epoch=_EPOCH
            ),
        )
        return
    if (from_state, to_state) == ("live", "exited"):
        assert manager.mark_exited(row.id)
        return
    if (from_state, to_state) == ("live", "orphaned"):
        assert manager.mark_orphaned(row.id)
        return
    if (from_state, to_state) == ("orphaned", "exited"):
        assert manager.mark_exited(row.id)
        return
    raise AssertionError(f"unhandled allowed edge {from_state}->{to_state}")


def test_pending_nullable_locator_and_atomic_promotion(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])
    assert pending.locator is None
    assert pending.locator_key is None
    assert pending.spawn_key is not None

    locator = _native_locator(host_terminal_id="%77")
    live = manager.promote_to_live(
        pending.id, locator=locator, locator_key=_native_key(locator), host_epoch=_EPOCH
    )
    assert live is not None
    assert live.state == "live"
    assert live.locator == locator
    assert live.locator_key == _native_key(locator)

    stale = manager.promote_to_live(
        pending.id, locator=locator, locator_key=_native_key(locator), host_epoch=_EPOCH
    )
    assert stale is None
    unchanged = manager.get(pending.id)
    assert unchanged is not None
    assert unchanged.state == "live"


def test_machine_id_is_required_and_local(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])
    assert pending.machine_id == LOCAL_MACHINE_ID
    with pytest.raises(NotNullViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, project_id
            ) VALUES (%s, 'native', 'gobby', 'pending', %s, %s)
            """,
            (str(uuid.uuid4()), f"gobby-{uuid.uuid4()}", sample_project["id"]),
        )


def test_native_locator_has_no_host_socket(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"], backend="native")
    host_terminal_id = str(uuid.uuid4())
    epoch = str(uuid.uuid4())
    locator = {"host_terminal_id": host_terminal_id}
    live = manager.promote_to_live(
        pending.id,
        locator=locator,
        locator_key=native_locator_key(epoch, host_terminal_id),
        host_epoch=epoch,
    )
    assert live is not None
    assert live.locator == locator
    assert "host_socket" not in live.locator

    attach = manager.attach_locator(live.id, live_host_epoch=epoch, socket_dir=tmp_path)
    assert attach.backend == "native"
    assert attach.host_socket == str(tmp_path / "gterm-frames.sock")
    assert attach.frame_host_epoch == epoch

    with pytest.raises(HostEpochMismatchError):
        manager.attach_locator(live.id, live_host_epoch=str(uuid.uuid4()), socket_dir=tmp_path)


def test_attach_locator_uses_native_frame_host_epoch(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"], backend="native")
    native_epoch = str(uuid.uuid4())
    host_terminal_id = str(uuid.uuid4())
    live = manager.promote_to_live(
        pending.id,
        locator={"host_terminal_id": host_terminal_id},
        locator_key=native_locator_key(native_epoch, host_terminal_id),
        host_epoch=native_epoch,
    )
    assert live is not None
    native_attach = manager.attach_locator(
        live.id, live_host_epoch=native_epoch, socket_dir=tmp_path
    )
    assert native_attach.frame_host_epoch == native_epoch


def test_automatic_write_quarantine_round_trips(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])
    assert pending.automatic_write_quarantined_at is None
    assert pending.automatic_write_quarantine_action_key is None

    manager.set_automatic_write_quarantine(pending.id, "wake-enter")
    loaded = manager.get(pending.id)
    assert loaded is not None
    assert loaded.automatic_write_quarantine_action_key == "wake-enter"
    assert loaded.automatic_write_quarantined_at is not None

    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            UPDATE terminals
            SET automatic_write_quarantined_at = now(),
                automatic_write_quarantine_action_key = NULL
            WHERE id = %s
            """,
            (pending.id,),
        )
    manager.clear_automatic_write_quarantine(pending.id)
    cleared = manager.get(pending.id)
    assert cleared is not None
    assert cleared.automatic_write_quarantined_at is None
    assert cleared.automatic_write_quarantine_action_key is None


def test_project_id_is_required_and_survives_session_clear(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    manager = _manager(temp_db)
    session = session_manager.register(
        external_id="term-project-session",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    pending = manager.create_pending(
        terminal_id=str(uuid.uuid4()),
        project_id=sample_project["id"],
        backend="native",
        ownership="gobby",
        spawn_key=f"gobby-{uuid.uuid4()}",
        machine_id=require_machine_id(),
        session_id=session.id,
    )
    temp_db.execute(
        "UPDATE terminals SET session_id = NULL, agent_run_id = NULL WHERE id = %s",
        (pending.id,),
    )
    listed = manager.list_by_project(sample_project["id"])
    assert pending.id in {row.id for row in listed}
    with pytest.raises(NotNullViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id
            ) VALUES (%s, 'native', 'gobby', 'pending', %s, %s)
            """,
            (str(uuid.uuid4()), f"gobby-{uuid.uuid4()}", require_machine_id()),
        )


def test_unresolved_writes_are_bounded(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])
    with pytest.raises(UnresolvedWriteCapacityError):
        manager.persist_unresolved_write(
            pending.id,
            "k" * (UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES + 1),
            "automatic",
            daemon_epoch="test-epoch",
        )

    for index in range(UNRESOLVED_WRITE_MAX_ENTRIES):
        key = f"{index:02d}" + ("a" * (UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES - 2))
        manager.persist_unresolved_write(
            pending.id,
            key,
            "automatic",
            daemon_epoch="test-epoch",
        )
    with pytest.raises(UnresolvedWriteCapacityError):
        manager.persist_unresolved_write(
            pending.id,
            "overflow-key",
            "automatic",
            daemon_epoch="test-epoch",
        )

    loaded = manager.get(pending.id)
    assert loaded is not None
    assert len(loaded.unresolved_writes) == UNRESOLVED_WRITE_MAX_ENTRIES

    huge_origin = "o" * UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES
    other = _create_pending(manager, sample_project["id"])
    with pytest.raises(UnresolvedWriteCapacityError):
        manager.persist_unresolved_write(
            other.id,
            "big",
            huge_origin,
            daemon_epoch="test-epoch",
        )


def test_latch_entries_carry_daemon_epoch(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])

    with pytest.raises(ValueError, match="daemon_epoch"):
        manager.persist_unresolved_write(
            pending.id,
            "ws:empty",
            "operator",
            daemon_epoch="",
        )

    manager.persist_unresolved_write(
        pending.id,
        "ws:attachment:1",
        "operator",
        daemon_epoch="epoch-current",
    )
    loaded = _manager(temp_db).get(pending.id)
    assert loaded is not None
    entry = loaded.unresolved_writes["ws:attachment:1"]
    assert entry["origin"] == "operator"
    assert entry["daemon_epoch"] == "epoch-current"
    assert isinstance(entry["at"], str)


def test_unresolved_write_fingerprint_round_trip(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    fingerprint = "a" * 64
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])

    manager.persist_unresolved_write(
        pending.id,
        "mcp-send-keys:session:key",
        "daemon",
        daemon_epoch="test-epoch",
        payload_fingerprint=fingerprint,
    )

    loaded = _manager(temp_db).get(pending.id)
    assert loaded is not None
    assert (
        loaded.unresolved_writes["mcp-send-keys:session:key"]["payload_fingerprint"] == fingerprint
    )

    memory = MemoryTerminalStore(make_memory_terminal())
    memory_terminal = next(iter(memory.rows.values()))
    memory.persist_unresolved_write(
        memory_terminal.id,
        "mcp-send-keys:session:key",
        "daemon",
        daemon_epoch="test-epoch",
        payload_fingerprint=fingerprint,
    )
    assert (
        memory_terminal.unresolved_writes["mcp-send-keys:session:key"]["payload_fingerprint"]
        == fingerprint
    )

    oversized = _create_pending(manager, sample_project["id"])
    with pytest.raises(UnresolvedWriteCapacityError):
        manager.persist_unresolved_write(
            oversized.id,
            "mcp-send-keys:oversized:key",
            "daemon",
            daemon_epoch="test-epoch",
            payload_fingerprint="a" * UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES,
        )


def test_orphan_sweep_clears_only_dead_epochs_on_this_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    current_epoch = "epoch-current"
    old_epoch = "epoch-old"
    other_machine_id = "22000000-0000-4000-8000-000000000002"
    manager = _manager(temp_db)
    local = _create_pending(manager, sample_project["id"])
    LocalMachineManager(temp_db).upsert_seen(other_machine_id, TEST_USER_ID)
    with patch("gobby.utils.machine_id._cached_machine_id", other_machine_id):
        other = _create_pending(manager, sample_project["id"])

    for action_key, daemon_epoch in (
        ("ws:old:1", old_epoch),
        ("ws:current:1", current_epoch),
        ("wake:automatic", old_epoch),
    ):
        manager.persist_unresolved_write(
            local.id,
            action_key,
            "operator" if action_key.startswith("ws:") else "automatic",
            daemon_epoch=daemon_epoch,
        )
    manager.persist_unresolved_write(
        other.id,
        "ws:other-machine:1",
        "operator",
        daemon_epoch=old_epoch,
    )

    reloaded = _manager(temp_db)
    assert reloaded.clear_orphaned_attachment_writes(LOCAL_MACHINE_ID, current_epoch) == 1
    local_after = reloaded.get(local.id)
    other_after = reloaded.get(other.id)
    assert local_after is not None
    assert other_after is not None
    assert set(local_after.unresolved_writes) == {"ws:current:1", "wake:automatic"}
    assert set(other_after.unresolved_writes) == {"ws:other-machine:1"}

    memory_local = make_memory_terminal(terminal_id="memory-local")
    memory_local.machine_id = LOCAL_MACHINE_ID
    memory_other = make_memory_terminal(terminal_id="memory-other")
    memory_other.machine_id = other_machine_id
    memory = MemoryTerminalStore(memory_local)
    memory.rows[memory_other.id] = memory_other
    memory.persist_unresolved_write(
        memory_local.id,
        "ws:old:1",
        "operator",
        daemon_epoch=old_epoch,
    )
    memory.persist_unresolved_write(
        memory_local.id,
        "ws:current:1",
        "operator",
        daemon_epoch=current_epoch,
    )
    memory.persist_unresolved_write(
        memory_other.id,
        "ws:other-machine:1",
        "operator",
        daemon_epoch=old_epoch,
    )

    assert memory.clear_orphaned_attachment_writes(LOCAL_MACHINE_ID, current_epoch) == 1
    assert set(memory.rows[memory_local.id].unresolved_writes) == {"ws:current:1"}
    assert set(memory.rows[memory_other.id].unresolved_writes) == {"ws:other-machine:1"}


def test_native_epoch_machine_ids_span_states_and_machines(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """The adoption matrix reads every native row bound to an epoch (plan 1.2.2)."""
    other_machine_id = "22000000-0000-4000-8000-000000000002"
    manager = _manager(temp_db)
    LocalMachineManager(temp_db).upsert_seen(other_machine_id, TEST_USER_ID)
    epoch = str(uuid.uuid4())

    def live_native_row() -> str:
        row = _create_pending(manager, sample_project["id"], backend="native")
        host_terminal_id = str(uuid.uuid4())
        promoted = manager.promote_to_live(
            row.id,
            locator={"host_terminal_id": host_terminal_id},
            locator_key=native_locator_key(epoch, host_terminal_id),
            host_epoch=epoch,
        )
        assert promoted is not None
        return row.id

    _create_pending(manager, sample_project["id"])
    live_native_row()
    assert manager.native_epoch_machine_ids(epoch) == {LOCAL_MACHINE_ID}

    with patch("gobby.utils.machine_id._cached_machine_id", other_machine_id):
        foreign_id = live_native_row()
        assert manager.mark_orphaned(foreign_id) is not None
    assert manager.native_epoch_machine_ids(epoch) == {LOCAL_MACHINE_ID, other_machine_id}
    assert manager.native_epoch_machine_ids(str(uuid.uuid4())) == set()


def test_title_is_byte_bounded(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    exact = "a" * TITLE_MAX_BYTES
    pending = manager.create_pending(
        terminal_id=str(uuid.uuid4()),
        project_id=sample_project["id"],
        backend="native",
        ownership="gobby",
        spawn_key=f"gobby-{uuid.uuid4()}",
        machine_id=require_machine_id(),
        title=exact,
    )
    assert pending.title == exact
    truncated = truncate_title("é" * (TITLE_MAX_BYTES + 10))
    assert truncated is not None
    assert len(truncated.encode("utf-8")) <= TITLE_MAX_BYTES
    with pytest.raises(CheckViolation):
        temp_db.execute(
            "UPDATE terminals SET title = %s WHERE id = %s",
            ("b" * (TITLE_MAX_BYTES + 1), pending.id),
        )


def test_attempt_generation_and_native_process_metadata(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"], backend="native")
    assert pending.attempt_generation == 1
    assert pending.attempt_started_at is not None
    process = {"host_terminal_id": "ht-process", "pgid": 4242, "start_time": 1_700_000_000}
    manager.record_process(
        pending.id,
        process,
        attempt_generation=pending.attempt_generation,
        attempt_started_at=pending.attempt_started_at,
    )
    host_terminal_id = str(uuid.uuid4())
    epoch = str(uuid.uuid4())
    live = manager.promote_to_live(
        pending.id,
        locator={"host_terminal_id": host_terminal_id},
        locator_key=native_locator_key(epoch, host_terminal_id),
        host_epoch=epoch,
    )
    assert live is not None
    assert live.process == process


def test_attempt_started_at_is_attempt_specific(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])
    original = pending.attempt_started_at
    assert original is not None
    temp_db.execute(
        "UPDATE terminals SET updated_at = %s WHERE id = %s",
        (datetime.now(UTC) + timedelta(hours=2), pending.id),
    )
    stale = manager.list_stale_pending(max_age_seconds=60)
    assert pending.id not in {row.id for row in stale}
    bumped = manager.bump_attempt_generation(pending.id)
    assert bumped is not None
    assert bumped.attempt_generation == 2
    assert bumped.attempt_started_at > original
    aged = manager.list_stale_pending(max_age_seconds=0)
    assert pending.id in {row.id for row in aged}


def test_cas_stale_transitions_affect_zero_rows(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])
    assert manager.mark_exited(pending.id) is None
    still = manager.get(pending.id)
    assert still is not None
    assert still.state == "pending"
    locator = _native_locator(host_terminal_id="%cas")
    live = manager.promote_to_live(
        pending.id, locator=locator, locator_key=_native_key(locator), host_epoch=_EPOCH
    )
    assert live is not None
    assert (
        manager.promote_to_live(
            pending.id, locator=locator, locator_key=_native_key(locator), host_epoch=_EPOCH
        )
        is None
    )
    assert manager.fail_pending(pending.id) is None


def test_settle_exit_is_guarded_cas(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"], backend="native")
    process = {"host_terminal_id": "ht-pending", "pgid": 101, "start_time": 201}
    recorded = manager.record_process(
        pending.id,
        process,
        attempt_generation=pending.attempt_generation,
        attempt_started_at=pending.attempt_started_at,
    )
    assert recorded is not None
    assert manager.settle_exit(pending.id, "ht-other") is None
    still_pending = manager.get(pending.id)
    assert still_pending is not None
    assert still_pending.state == "pending"
    exited = manager.settle_exit(pending.id, "ht-pending")
    assert exited is not None
    assert exited.state == "exited"

    live_pending = _create_pending(manager, sample_project["id"], backend="native")
    manager.record_process(
        live_pending.id,
        {"host_terminal_id": "ht-live"},
        attempt_generation=live_pending.attempt_generation,
        attempt_started_at=live_pending.attempt_started_at,
    )
    live = manager.promote_to_live(
        live_pending.id,
        locator={"host_terminal_id": "ht-live"},
        locator_key=native_locator_key("epoch-live", "ht-live"),
        host_epoch="epoch-live",
    )
    assert live is not None
    assert manager.settle_exit(live.id, "ht-wrong") is None
    live_exited = manager.settle_exit(live.id, "ht-live")
    assert live_exited is not None
    assert live_exited.state == "exited"


@pytest.mark.asyncio
async def test_settle_lock_serializes_promotion_and_exit(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.agents.spawn_executor import settle_promotion

    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"], backend="native")
    manager.record_process(
        pending.id,
        {"host_terminal_id": "ht-race", "pgid": 102, "start_time": 202},
        attempt_generation=pending.attempt_generation,
        attempt_started_at=pending.attempt_started_at,
    )

    async def settle_exit() -> Terminal | None:
        async with manager.settle_lock(pending.id):
            return await asyncio.to_thread(manager.settle_exit, pending.id, "ht-race")

    promoted, exited = await asyncio.gather(
        settle_promotion(
            manager,
            pending.id,
            locator={"host_terminal_id": "ht-race"},
            locator_key=native_locator_key("epoch-race", "ht-race"),
            host_epoch="epoch-race",
        ),
        settle_exit(),
    )
    assert promoted is not None or exited is not None
    settled = manager.get(pending.id)
    assert settled is not None
    assert settled.state == "exited"


def test_record_process_is_attempt_cas_merge(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"], backend="native")
    with pytest.raises(ValueError):
        manager.record_process(
            pending.id,
            {"pgid": 103, "start_time": 203},
            attempt_generation=pending.attempt_generation,
            attempt_started_at=pending.attempt_started_at,
        )

    recorded = manager.record_process(
        pending.id,
        {"host_terminal_id": "ht-cas", "reservation_id": "rsv"},
        attempt_generation=pending.attempt_generation,
        attempt_started_at=pending.attempt_started_at,
    )
    assert recorded is not None
    assert recorded.process == {"host_terminal_id": "ht-cas", "reservation_id": "rsv"}

    stale_generation = pending.attempt_generation
    stale_started_at = pending.attempt_started_at
    bumped = manager.bump_attempt_generation(pending.id)
    assert bumped is not None
    assert (
        manager.record_process(
            pending.id,
            {"host_terminal_id": "ht-stale"},
            attempt_generation=stale_generation,
            attempt_started_at=stale_started_at,
        )
        is None
    )
    merged = manager.merge_process_reap_record(pending.id, pgid=104, start_time=204)
    assert merged is not None
    assert merged.process == {"reservation_id": "rsv", "pgid": 104, "start_time": 204}


def test_bump_attempt_drops_host_id_and_retry_refuses_settled(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"], backend="native")
    recorded = manager.record_process(
        pending.id,
        {"host_terminal_id": "ht-settled", "pgid": 105},
        attempt_generation=pending.attempt_generation,
        attempt_started_at=pending.attempt_started_at,
    )
    assert recorded is not None
    assert manager.retry_attempt_unsettled(pending.id, pending.attempt_generation) is None

    bumped = manager.bump_attempt_generation(pending.id)
    assert bumped is not None
    assert bumped.process == {"pgid": 105}

    fresh = _create_pending(manager, sample_project["id"], backend="native")
    fresh_generation = fresh.attempt_generation
    retried = manager.retry_attempt_unsettled(fresh.id, fresh_generation)
    assert retried is not None
    assert retried.attempt_generation == fresh_generation + 1
