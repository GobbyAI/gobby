"""Terminals table and TerminalManager storage contract (plan 2.1)."""

from __future__ import annotations

import inspect
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
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import (
    ALLOWED_EDGES,
    TERMINAL_STATES,
    TITLE_MAX_BYTES,
    UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES,
    UNRESOLVED_WRITE_MAX_ENTRIES,
    UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES,
    HostEpochMismatchError,
    IllegalTerminalTransitionError,
    ProjectOwnershipConflictError,
    Terminal,
    TerminalManager,
    UnresolvedWriteCapacityError,
    native_locator_key,
    parse_tmux_generation,
    tmux_locator_key,
    truncate_title,
)
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
_SOCKET = "/private/tmp/tmux-501/default"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _tmux_locator(
    *,
    socket_path: str = _SOCKET,
    server_pid: int = 1658,
    server_start_time: int = 1784592177,
    pane_id: str = "%12",
) -> dict[str, object]:
    return {
        "socket_path": socket_path,
        "server_pid": server_pid,
        "server_start_time": server_start_time,
        "pane_id": pane_id,
    }


def _tmux_key(locator: dict[str, object]) -> str:
    server_pid = locator["server_pid"]
    server_start_time = locator["server_start_time"]
    if not isinstance(server_pid, int) or not isinstance(server_start_time, int):
        raise TypeError("tmux locator pid and start_time must be int")
    return tmux_locator_key(
        socket_path=str(locator["socket_path"]),
        server_pid=server_pid,
        server_start_time=server_start_time,
        pane_id=str(locator["pane_id"]),
    )


def _manager(temp_db: HubDatabase) -> TerminalManager:
    return TerminalManager(temp_db)


def _create_pending(
    manager: TerminalManager,
    project_id: str,
    *,
    backend: str = "tmux",
    terminal_id: str | None = None,
    spawn_key: str | None = None,
) -> Terminal:
    tid = terminal_id or str(uuid.uuid4())
    key = spawn_key
    if key is None:
        key = tid if backend == "native" else f"gobby-{tid}"
    return manager.create_pending(
        terminal_id=tid,
        project_id=project_id,
        backend=backend,
        ownership="gobby",
        spawn_key=key,
        machine_id=require_machine_id(),
    )


def test_failed_spawn_leaves_reapable_pending_row(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"])

    stale = manager.list_stale_pending(max_age_seconds=0)
    assert pending.id in {row.id for row in stale}
    assert pending.spawn_key == f"gobby-{pending.id}"
    assert pending.locator is None
    assert pending.backend == "tmux"

    native = _create_pending(manager, sample_project["id"], backend="native")
    assert native.spawn_key == native.id
    assert native.id in {row.id for row in manager.list_stale_pending(max_age_seconds=0)}


def test_locator_key_uniqueness_replay_and_server_generation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    locator = _tmux_locator()
    first = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=locator,
        locator_key=_tmux_key(locator),
        session_name="sess-a",
        window_id="@1",
        title="one",
    )
    replay = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=locator,
        locator_key=_tmux_key(locator),
        session_name="sess-a-renamed",
        window_id="@1",
        title="one",
    )
    assert replay.id == first.id
    assert replay.session_name == "sess-a-renamed"

    other_pid = _tmux_locator(server_pid=9999)
    other = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=other_pid,
        locator_key=_tmux_key(other_pid),
        session_name="sess-b",
        window_id="@1",
        title="pid",
    )
    assert other.id != first.id

    pid_reuse = _tmux_locator(server_start_time=1784592178)
    reused = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=pid_reuse,
        locator_key=_tmux_key(pid_reuse),
        session_name="sess-c",
        window_id="@1",
        title="start",
    )
    assert reused.id != first.id

    manager.mark_exited(first.id)
    rediscovered = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=locator,
        locator_key=_tmux_key(locator),
        session_name="sess-a",
        window_id="@1",
        title="again",
    )
    assert rediscovered.id != first.id


def test_pane_identity_survives_rename_move_break_and_grouping(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    locator = _tmux_locator(pane_id="%12")
    key = _tmux_key(locator)
    original = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=locator,
        locator_key=key,
        session_name="dev",
        window_id="@1",
        title="before",
    )
    mutations = [
        ("renamed-session", "@1", "after-rename"),
        ("renamed-session", "@2", "after-move"),
        ("broken", "@3", "after-break"),
        ("grouped-alias", "@3", "grouped"),
    ]
    for session_name, window_id, title in mutations:
        current = manager.upsert_external(
            machine_id=require_machine_id(),
            project_id=sample_project["id"],
            backend="tmux",
            locator=locator,
            locator_key=key,
            session_name=session_name,
            window_id=window_id,
            title=title,
        )
        assert current.id == original.id
        assert current.session_name == session_name
        assert current.window_id == window_id
        assert current.title == title
    count = temp_db.fetchone("SELECT COUNT(*) AS n FROM terminals WHERE locator_key = %s", (key,))
    assert count is not None
    assert count["n"] == 1


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
            ) VALUES (%s, 'tmux', 'gobby', 'live', %s, %s, %s)
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
            ) VALUES (%s, 'tmux', 'gobby', 'orphaned', %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                f"gobby-{uuid.uuid4()}",
                require_machine_id(),
                sample_project["id"],
            ),
        )

    live = _create_pending(manager, sample_project["id"])
    locator = _tmux_locator(pane_id="%99")
    manager.promote_to_live(live.id, locator=locator, locator_key=_tmux_key(locator))
    manager.mark_exited(live.id)
    after = manager.get(live.id)
    assert after is not None
    assert after.locator == locator
    assert after.locator_key == _tmux_key(locator)


def test_spawn_key_domain_matches_ownership(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    locator = _tmux_locator(pane_id="%1")
    discovered = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=locator,
        locator_key=_tmux_key(locator),
        session_name="ext",
        window_id="@1",
        title="ext",
    )
    assert discovered.spawn_key is None
    assert discovered.ownership == "external"

    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, machine_id, project_id, locator, locator_key
            ) VALUES (%s, 'tmux', 'gobby', 'live', %s, %s, %s::jsonb, %s)
            """,
            (
                str(uuid.uuid4()),
                require_machine_id(),
                sample_project["id"],
                json.dumps(locator),
                _tmux_key(locator) + "-gobby-missing-spawn",
            ),
        )
    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id,
                locator, locator_key
            ) VALUES (%s, 'tmux', 'external', 'live', 'should-not-exist', %s, %s, %s::jsonb, %s)
            """,
            (
                str(uuid.uuid4()),
                require_machine_id(),
                sample_project["id"],
                json.dumps(_tmux_locator(pane_id="%2")),
                _tmux_key(_tmux_locator(pane_id="%2")),
            ),
        )

    second = _tmux_locator(pane_id="%3")
    other = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=second,
        locator_key=_tmux_key(second),
        session_name="ext-2",
        window_id="@1",
        title="ext-2",
    )
    assert other.id != discovered.id

    one = _create_pending(manager, sample_project["id"], spawn_key="unique-spawn")
    with pytest.raises(UniqueViolation):
        _create_pending(manager, sample_project["id"], spawn_key="unique-spawn")
    assert one.spawn_key == "unique-spawn"


def test_generation_is_single_read_and_revalidated_before_bind(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    source = inspect.getsource(parse_tmux_generation)
    assert "ps " not in source
    assert "/proc" not in source
    manager_source = inspect.getsource(TerminalManager)
    assert "ps -o" not in manager_source
    assert "/proc/" not in manager_source

    parsed = parse_tmux_generation(f"{_SOCKET}|1658|1784592177|%12")
    assert parsed == _tmux_locator()

    manager = _manager(temp_db)
    locator = _tmux_locator()
    row = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=locator,
        locator_key=_tmux_key(locator),
        session_name="live",
        window_id="@1",
        title="live",
    )
    live_mismatch = parse_tmux_generation(f"{_SOCKET}|1658|1784599999|%12")
    bound = manager.revalidate_tmux_generation(row.id, live_mismatch)
    assert bound is None
    exited = manager.get(row.id)
    assert exited is not None
    assert exited.state == "exited"
    rediscovered = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=live_mismatch,
        locator_key=_tmux_key(live_mismatch),
        session_name="live",
        window_id="@1",
        title="new-gen",
    )
    assert rediscovered.id != row.id


def test_row_and_edge_constraints_reject_illegal_variants(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    locator = _tmux_locator(pane_id="%edge")
    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id,
                locator, locator_key
            ) VALUES (%s, 'tmux', 'gobby', 'pending', %s, %s, %s, %s::jsonb, %s)
            """,
            (
                str(uuid.uuid4()),
                f"gobby-{uuid.uuid4()}",
                require_machine_id(),
                sample_project["id"],
                json.dumps(locator),
                _tmux_key(locator),
            ),
        )
    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id, locator
            ) VALUES (%s, 'tmux', 'gobby', 'exited', %s, %s, %s, %s::jsonb)
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
            ) VALUES (%s, 'tmux', 'external', 'pending', %s, %s, %s::jsonb, %s)
            """,
            (
                str(uuid.uuid4()),
                require_machine_id(),
                sample_project["id"],
                json.dumps(locator),
                _tmux_key(locator) + "-ext",
            ),
        )
    with pytest.raises(CheckViolation):
        temp_db.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id, host_epoch
            ) VALUES (%s, 'tmux', 'gobby', 'pending', %s, %s, %s, %s)
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
        locator = _tmux_locator(pane_id=f"%{uuid.uuid4().hex[:6]}")
        promoted = manager.promote_to_live(row.id, locator=locator, locator_key=_tmux_key(locator))
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
        locator = _tmux_locator(pane_id=f"%{uuid.uuid4().hex[:6]}")
        assert manager.promote_to_live(row.id, locator=locator, locator_key=_tmux_key(locator))
        return
    if (from_state, to_state) == ("pending", "exited"):
        assert manager.fail_pending(row.id)
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

    locator = _tmux_locator(pane_id="%77")
    live = manager.promote_to_live(pending.id, locator=locator, locator_key=_tmux_key(locator))
    assert live is not None
    assert live.state == "live"
    assert live.locator == locator
    assert live.locator_key == _tmux_key(locator)

    stale = manager.promote_to_live(pending.id, locator=locator, locator_key=_tmux_key(locator))
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
            ) VALUES (%s, 'tmux', 'gobby', 'pending', %s, %s)
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


def test_attach_locator_frame_host_epoch_by_backend(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    manager = _manager(temp_db)
    adopted_epoch = str(uuid.uuid4())
    tmux_locator = _tmux_locator(pane_id="%attach")
    tmux_row = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=tmux_locator,
        locator_key=_tmux_key(tmux_locator),
        session_name="tmux",
        window_id="@1",
        title="tmux",
    )
    assert tmux_row.host_epoch is None
    tmux_attach = manager.attach_locator(
        tmux_row.id, live_host_epoch=adopted_epoch, socket_dir=tmp_path
    )
    assert tmux_attach.frame_host_epoch == adopted_epoch
    assert tmux_attach.host_socket == str(tmp_path / "gterm-frames.sock")
    assert tmux_attach.server_pid == tmux_locator["server_pid"]
    assert tmux_attach.server_start_time == tmux_locator["server_start_time"]
    stored = manager.get(tmux_row.id)
    assert stored is not None
    assert stored.host_epoch is None

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
        backend="tmux",
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
            ) VALUES (%s, 'tmux', 'gobby', 'pending', %s, %s)
            """,
            (str(uuid.uuid4()), f"gobby-{uuid.uuid4()}", require_machine_id()),
        )


def test_external_locator_project_ownership_is_immutable(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    project_manager: LocalProjectManager,
) -> None:
    manager = _manager(temp_db)
    other = project_manager.create(name="other-project")
    locator = _tmux_locator(pane_id="%own")
    key = _tmux_key(locator)
    first = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=locator,
        locator_key=key,
        session_name="owner",
        window_id="@1",
        title="owner",
    )
    same = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=sample_project["id"],
        backend="tmux",
        locator=locator,
        locator_key=key,
        session_name="owner-refresh",
        window_id="@1",
        title="owner",
    )
    assert same.id == first.id
    assert same.project_id == sample_project["id"]

    with pytest.raises(ProjectOwnershipConflictError) as raised:
        manager.upsert_external(
            machine_id=require_machine_id(),
            project_id=other.id,
            backend="tmux",
            locator=locator,
            locator_key=key,
            session_name="thief",
            window_id="@1",
            title="thief",
        )
    assert first.id not in str(raised.value)
    assert "thief" not in str(raised.value)
    occupying = manager.get(first.id)
    assert occupying is not None
    assert occupying.project_id == sample_project["id"]
    assert occupying.title == "owner"

    manager.mark_exited(first.id)
    other_row = manager.upsert_external(
        machine_id=require_machine_id(),
        project_id=other.id,
        backend="tmux",
        locator=locator,
        locator_key=key,
        session_name="later",
        window_id="@1",
        title="later",
    )
    assert other_row.id != first.id
    assert other_row.project_id == other.id


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
        )

    for index in range(UNRESOLVED_WRITE_MAX_ENTRIES):
        key = f"{index:02d}" + ("a" * (UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES - 2))
        manager.persist_unresolved_write(pending.id, key, "automatic")
    with pytest.raises(UnresolvedWriteCapacityError):
        manager.persist_unresolved_write(pending.id, "overflow-key", "automatic")

    loaded = manager.get(pending.id)
    assert loaded is not None
    assert len(loaded.unresolved_writes) == UNRESOLVED_WRITE_MAX_ENTRIES

    huge_origin = "o" * UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES
    other = _create_pending(manager, sample_project["id"])
    with pytest.raises(UnresolvedWriteCapacityError):
        manager.persist_unresolved_write(other.id, "big", huge_origin)


def test_title_is_byte_bounded(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = _manager(temp_db)
    exact = "a" * TITLE_MAX_BYTES
    pending = manager.create_pending(
        terminal_id=str(uuid.uuid4()),
        project_id=sample_project["id"],
        backend="tmux",
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
    process = {"pgid": 4242, "start_time": 1_700_000_000}
    manager.record_process(pending.id, process)
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
    tmux_pending = _create_pending(manager, sample_project["id"])
    with pytest.raises(CheckViolation):
        temp_db.execute(
            "UPDATE terminals SET process = %s::jsonb WHERE id = %s",
            (json.dumps(process), tmux_pending.id),
        )


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
    locator = _tmux_locator(pane_id="%cas")
    live = manager.promote_to_live(pending.id, locator=locator, locator_key=_tmux_key(locator))
    assert live is not None
    assert (
        manager.promote_to_live(pending.id, locator=locator, locator_key=_tmux_key(locator)) is None
    )
    assert manager.fail_pending(pending.id) is None
