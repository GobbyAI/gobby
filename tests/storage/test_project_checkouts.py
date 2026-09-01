"""Tests for LocalProjectCheckoutManager and checkout-free sentinel IDs."""

from __future__ import annotations

import ast
import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import (
    CheckoutConflictError,
    CheckoutNotFoundError,
    CheckoutRootTakenError,
    CheckoutSentinelRejectedError,
    LocalProjectCheckoutManager,
    MissingMachineContextError,
    OverlayRegistrationRejectedError,
    ProjectCheckout,
    require_root,
    resolve_operation_root,
)
from gobby.storage.projects import (
    CHECKOUT_FREE_PROJECT_IDS,
    GLOBAL_PROJECT_ID,
    MIGRATED_PROJECT_ID,
    ORPHANED_PROJECT_ID,
    PERSONAL_PROJECT_ID,
    LocalProjectManager,
)
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_checkout_free_project_ids_pin_the_four_sentinels() -> None:
    """CHECKOUT_FREE_PROJECT_IDS pins the four checkout-free sentinel UUIDs."""
    assert MIGRATED_PROJECT_ID == "00000000-0000-0000-0000-000000000001"
    assert CHECKOUT_FREE_PROJECT_IDS == frozenset(
        {
            ORPHANED_PROJECT_ID,
            MIGRATED_PROJECT_ID,
            GLOBAL_PROJECT_ID,
            PERSONAL_PROJECT_ID,
        }
    )
    pinned = ProjectCheckout.from_row(
        {
            "machine_id": "m",
            "project_id": MIGRATED_PROJECT_ID,
            "root_path": "/pinned",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    assert pinned.root_path == "/pinned"


def _insert_machine(db: HubDatabase, machine_id: str) -> None:
    db.execute(
        "INSERT INTO machines (id, hostname, owner_user_id) VALUES (%s, %s, %s)",
        (machine_id, f"host-{machine_id}", TEST_USER_ID),
    )


def _manager(db: HubDatabase) -> LocalProjectCheckoutManager:
    return LocalProjectCheckoutManager(db)


def _seed_index_state(
    db: HubDatabase,
    machine_id: str,
    project_id: str,
    root_path: str,
    *,
    file_path: str = "src/app.py",
) -> str:
    content_hash = "sha256:indexed"
    file_id = str(uuid.uuid5(uuid.UUID(project_id), f"{file_path}:{content_hash}"))
    db.execute(
        "INSERT INTO code_indexed_projects (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
        (project_id,),
    )
    db.execute(
        """
        INSERT INTO code_indexed_files (
            id, project_id, file_path, language, content_hash,
            symbol_count, byte_size, graph_synced, vectors_synced
        ) VALUES (%s, %s, %s, 'python', %s, 1, 10, true, true)
        ON CONFLICT (id) DO NOTHING
        """,
        (file_id, project_id, file_path, content_hash),
    )
    db.execute(
        """
        INSERT INTO code_indexed_project_states (
            machine_id, project_id, root_path, total_files, total_symbols
        ) VALUES (%s, %s, %s, 1, 1)
        """,
        (machine_id, project_id, root_path),
    )
    db.execute(
        """
        INSERT INTO code_indexed_file_states (
            machine_id, project_id, file_path, content_hash
        ) VALUES (%s, %s, %s, %s)
        """,
        (machine_id, project_id, file_path, content_hash),
    )
    return file_id


def test_get_and_list_are_empty_until_register(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    manager = _manager(temp_db)
    project_id = sample_project["id"]

    assert manager.get(machine_id, project_id) is None
    assert manager.list_for_machine(machine_id) == []


def test_register_is_idempotent_for_the_same_root(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Idempotent register returns created=False without bumping updated_at."""
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    manager = _manager(temp_db)
    project_id = sample_project["id"]
    unix_root = "/Users/josh/work/repo"

    created, first_insert = manager.register(machine_id, project_id, unix_root)
    assert first_insert is True
    assert created.root_path == unix_root
    assert created.machine_id == machine_id
    assert created.project_id == project_id

    again, second_insert = manager.register(machine_id, project_id, unix_root)
    assert second_insert is False
    assert again.updated_at == created.updated_at
    assert manager.get(machine_id, project_id) == created
    listed = manager.list_for_machine(machine_id)
    assert [row.root_path for row in listed] == [unix_root]


def test_register_keeps_windows_root_strings_opaque(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Windows-style root_path round-trips without separator rewriting."""
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    manager = _manager(temp_db)
    windows_root = r"C:\work\repo"

    checkout, created = manager.register(machine_id, sample_project["id"], windows_root)
    assert created is True
    loaded = manager.get(machine_id, sample_project["id"])
    assert loaded is not None
    assert loaded.root_path == windows_root
    assert checkout.root_path == windows_root


def test_register_raises_conflict_for_a_different_root(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Same machine+project and a different root is CheckoutConflictError."""
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    manager = _manager(temp_db)
    project_id = sample_project["id"]
    manager.register(machine_id, project_id, "/a/repo")

    with pytest.raises(CheckoutConflictError):
        manager.register(machine_id, project_id, "/b/repo")
    loaded = manager.get(machine_id, project_id)
    assert loaded is not None
    assert loaded.root_path == "/a/repo"


def test_register_raises_when_another_project_owns_the_root(
    temp_db: HubDatabase, project_manager: Any, sample_project: dict[str, Any]
) -> None:
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    manager = _manager(temp_db)
    other = project_manager.create(name="other-checkout-project")
    manager.register(machine_id, sample_project["id"], "/shared/root")

    with pytest.raises(CheckoutRootTakenError):
        manager.register(machine_id, other.id, "/shared/root")
    assert manager.get(machine_id, other.id) is None


def test_register_root_taken_inside_outer_transaction_raises_typed_error(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A root-taken register nested in a caller's transaction still raises the typed error."""
    manager = _manager(temp_db)
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    other = LocalProjectManager(temp_db).create(name=f"root-owner-{uuid.uuid4().hex[:8]}")
    manager.register(machine_id, other.id, "/shared/root")

    with pytest.raises(CheckoutRootTakenError), temp_db.transaction():
        manager.register(machine_id, sample_project["id"], "/shared/root")

    assert manager.get(machine_id, sample_project["id"]) is None
    assert manager.get(machine_id, other.id) is not None


def test_unregister_project_releases_roots_on_every_machine(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    """unregister_project drops the project's rows on every machine and frees the roots."""
    manager = _manager(temp_db)
    first_machine = str(uuid.uuid4())
    second_machine = str(uuid.uuid4())
    _insert_machine(temp_db, first_machine)
    _insert_machine(temp_db, second_machine)
    root = str(tmp_path / "shared-root")
    manager.register(first_machine, sample_project["id"], root)
    manager.register(second_machine, sample_project["id"], "/second/root")
    other = LocalProjectManager(temp_db).create(name=f"other-{uuid.uuid4().hex[:8]}")
    with pytest.raises(CheckoutRootTakenError):
        manager.register(first_machine, other.id, root)
    before = temp_db.fetchone(
        "SELECT count(*) AS n FROM project_checkouts WHERE project_id = %s",
        (sample_project["id"],),
    )
    assert before is not None
    assert int(before["n"]) >= 2

    assert manager.unregister_project(sample_project["id"]) == int(before["n"])

    assert manager.get(first_machine, sample_project["id"]) is None
    assert manager.get(second_machine, sample_project["id"]) is None
    reclaimed, created = manager.register(first_machine, other.id, root)
    assert created is True
    assert reclaimed.root_path == root
    assert manager.unregister_project(sample_project["id"]) == 0


@pytest.mark.parametrize("sentinel_id", sorted(CHECKOUT_FREE_PROJECT_IDS))
def test_register_and_rebind_refuse_checkout_free_sentinels(
    temp_db: HubDatabase, sentinel_id: str
) -> None:
    """Checkout-free sentinel ids cannot register or rebind a root."""
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    manager = _manager(temp_db)
    with pytest.raises(CheckoutSentinelRejectedError):
        manager.register(machine_id, sentinel_id, "/sentinel/root")
    with pytest.raises(CheckoutSentinelRejectedError):
        manager.rebind(machine_id, sentinel_id, "/sentinel/root")
    assert manager.get(machine_id, sentinel_id) is None
    mapped = ProjectCheckout.from_row(
        {
            "machine_id": machine_id,
            "project_id": sentinel_id,
            "root_path": "/sentinel/root",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    assert mapped.root_path == "/sentinel/root"


def test_rebind_inserts_noops_and_updates_roots(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """rebind inserts, no-ops the same root, then moves to a different root."""
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    manager = _manager(temp_db)
    project_id = sample_project["id"]

    inserted = manager.rebind(machine_id, project_id, "/first/root")
    assert inserted.root_path == "/first/root"
    same = manager.rebind(machine_id, project_id, "/first/root")
    assert same.updated_at == inserted.updated_at
    moved = manager.rebind(machine_id, project_id, "/second/root")
    assert moved.root_path == "/second/root"
    assert moved.updated_at >= inserted.updated_at


def test_rebind_raises_when_another_project_owns_the_root(
    temp_db: HubDatabase, project_manager: Any, sample_project: dict[str, Any]
) -> None:
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    manager = _manager(temp_db)
    other = project_manager.create(name="root-owner")
    manager.register(machine_id, other.id, "/taken/root")
    manager.register(machine_id, sample_project["id"], "/mine/root")
    before = manager.get(machine_id, sample_project["id"])
    assert before is not None

    with pytest.raises(CheckoutRootTakenError):
        manager.rebind(machine_id, sample_project["id"], "/taken/root")
    after = manager.get(machine_id, sample_project["id"])
    assert after is not None
    assert after.root_path == "/mine/root"
    assert after.updated_at == before.updated_at


def test_register_and_rebind_reject_machine_overlays(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """A worktree or clone path on the same machine is OverlayRegistrationRejectedError."""
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    manager = _manager(temp_db)
    project_id = sample_project["id"]
    worktree_path = "/tmp/wt-overlay"
    clone_path = "/tmp/clone-overlay"
    assert (
        temp_db.fetchone(
            "SELECT 1 FROM worktrees WHERE machine_id = %s AND worktree_path = %s",
            (machine_id, worktree_path),
        )
        is None
    )
    assert (
        temp_db.fetchone(
            "SELECT 1 FROM clones WHERE machine_id = %s AND clone_path = %s",
            (machine_id, clone_path),
        )
        is None
    )
    temp_db.execute(
        """
        INSERT INTO worktrees (
            id, project_id, machine_id, branch_name, worktree_path
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (str(uuid.uuid4()), project_id, machine_id, "task/overlay", worktree_path),
    )
    temp_db.execute(
        """
        INSERT INTO clones (
            id, project_id, machine_id, branch_name, clone_path
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (str(uuid.uuid4()), project_id, machine_id, "task/clone", clone_path),
    )

    with pytest.raises(OverlayRegistrationRejectedError):
        manager.register(machine_id, project_id, worktree_path)
    with pytest.raises(OverlayRegistrationRejectedError):
        manager.rebind(machine_id, project_id, clone_path)
    ordinary, created = manager.register(machine_id, project_id, "/ordinary/root")
    assert created is True
    assert ordinary.root_path == "/ordinary/root"


def test_rebind_insert_clears_mismatched_local_project_state(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent checkout invalidates stale local state in its insert transaction."""
    machine_id = str(uuid.uuid4())
    other_machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    _insert_machine(temp_db, other_machine_id)
    project_id = sample_project["id"]
    file_id = _seed_index_state(temp_db, machine_id, project_id, "/old/index-root")
    _seed_index_state(temp_db, other_machine_id, project_id, "/other/index-root")
    manager = _manager(temp_db)
    checkout = manager.rebind(machine_id, project_id, "/new/checkout-root")
    assert checkout.root_path == "/new/checkout-root"
    assert (
        temp_db.fetchone(
            """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
            (machine_id, project_id),
        )
        is None
    )
    assert (
        temp_db.fetchone(
            """
        SELECT file_path FROM code_indexed_file_states
        WHERE machine_id = %s AND project_id = %s
        """,
            (machine_id, project_id),
        )
        is None
    )
    assert temp_db.fetchone(
        """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
        (other_machine_id, project_id),
    ) == {"root_path": "/other/index-root"}
    assert temp_db.fetchone(
        "SELECT id FROM code_indexed_files WHERE id = %s",
        (file_id,),
    ) == {"id": file_id}

    no_state_machine = str(uuid.uuid4())
    _insert_machine(temp_db, no_state_machine)
    no_state = manager.rebind(no_state_machine, project_id, "/no/state")
    assert no_state.root_path == "/no/state"
    assert (
        temp_db.fetchone(
            """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
            (no_state_machine, project_id),
        )
        is None
    )

    matching_machine = str(uuid.uuid4())
    _insert_machine(temp_db, matching_machine)
    _seed_index_state(temp_db, matching_machine, project_id, "/matching/root")
    matching = manager.rebind(matching_machine, project_id, "/matching/root")
    assert matching.root_path == "/matching/root"
    assert temp_db.fetchone(
        """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
        (matching_machine, project_id),
    ) == {"root_path": "/matching/root"}

    same_root_machine = str(uuid.uuid4())
    _insert_machine(temp_db, same_root_machine)
    inserted = manager.rebind(same_root_machine, project_id, "/same/root")
    _seed_index_state(temp_db, same_root_machine, project_id, "/same/root")
    same = manager.rebind(same_root_machine, project_id, "/same/root")
    assert same.updated_at == inserted.updated_at
    assert temp_db.fetchone(
        """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
        (same_root_machine, project_id),
    ) == {"root_path": "/same/root"}

    moved_machine = str(uuid.uuid4())
    _insert_machine(temp_db, moved_machine)
    manager.register(moved_machine, project_id, "/move/old")
    _seed_index_state(temp_db, moved_machine, project_id, "/move/old")
    moved = manager.rebind(moved_machine, project_id, "/move/new")
    assert moved.root_path == "/move/new"
    assert (
        temp_db.fetchone(
            """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
            (moved_machine, project_id),
        )
        is None
    )

    rollback_machine = str(uuid.uuid4())
    _insert_machine(temp_db, rollback_machine)
    manager.register(rollback_machine, project_id, "/rollback/old")
    _seed_index_state(temp_db, rollback_machine, project_id, "/rollback/old")
    clear_index_state = manager._clear_index_state

    def _abort_after_clear(conn: Any, scoped_machine_id: str, scoped_project_id: str) -> None:
        clear_index_state(conn, scoped_machine_id, scoped_project_id)
        raise RuntimeError("abort rebind")

    monkeypatch.setattr(manager, "_clear_index_state", _abort_after_clear)
    with pytest.raises(RuntimeError, match="abort rebind"):
        manager.rebind(rollback_machine, project_id, "/rollback/new")
    rolled_back = manager.get(rollback_machine, project_id)
    assert rolled_back is not None
    assert rolled_back.root_path == "/rollback/old"
    assert temp_db.fetchone(
        """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
        (rollback_machine, project_id),
    ) == {"root_path": "/rollback/old"}
    monkeypatch.setattr(manager, "_clear_index_state", clear_index_state)
    assert manager.rebind(rollback_machine, project_id, "/rollback/new").root_path == (
        "/rollback/new"
    )

    concurrent_machine = str(uuid.uuid4())
    _insert_machine(temp_db, concurrent_machine)
    second = PostgresHubDatabase(temp_db.conninfo)
    barrier = threading.Barrier(2)
    roots = ("/concurrent/a", "/concurrent/b")
    results: list[str] = []
    errors: list[Exception] = []

    def _concurrent_rebind(db: HubDatabase, root: str) -> None:
        try:
            barrier.wait(timeout=10)
            checkout = LocalProjectCheckoutManager(db).rebind(concurrent_machine, project_id, root)
            results.append(checkout.root_path)
        except Exception as exc:
            errors.append(exc)

    try:
        threads = [
            threading.Thread(target=_concurrent_rebind, args=(temp_db, roots[0])),
            threading.Thread(target=_concurrent_rebind, args=(second, roots[1])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert errors == []
        assert set(results) == set(roots)
        listed = manager.list_for_machine(concurrent_machine)
        assert len(listed) == 1
        assert listed[0].root_path in roots
    finally:
        second.close()


def test_rebind_insert_preserves_matching_local_index_state(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    project_id = sample_project["id"]
    root_path = "/matching/root"
    _seed_index_state(temp_db, machine_id, project_id, root_path)

    checkout = _manager(temp_db).rebind(machine_id, project_id, root_path)

    assert checkout.root_path == root_path
    assert temp_db.fetchone(
        """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
        (machine_id, project_id),
    ) == {"root_path": root_path}
    assert temp_db.fetchone(
        """
        SELECT file_path FROM code_indexed_file_states
        WHERE machine_id = %s AND project_id = %s
        """,
        (machine_id, project_id),
    ) == {"file_path": "src/app.py"}


def test_rebind_different_root_clears_local_index_state(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    project_id = sample_project["id"]
    manager = _manager(temp_db)
    manager.register(machine_id, project_id, "/old/root")
    _seed_index_state(temp_db, machine_id, project_id, "/old/root")

    checkout = manager.rebind(machine_id, project_id, "/new/root")

    assert checkout.root_path == "/new/root"
    assert (
        temp_db.fetchone(
            """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
            (machine_id, project_id),
        )
        is None
    )
    assert (
        temp_db.fetchone(
            """
        SELECT file_path FROM code_indexed_file_states
        WHERE machine_id = %s AND project_id = %s
        """,
            (machine_id, project_id),
        )
        is None
    )


def test_failed_rebind_rolls_back_and_rerun_clears_state(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    project_id = sample_project["id"]
    manager = _manager(temp_db)
    manager.register(machine_id, project_id, "/old/root")
    _seed_index_state(temp_db, machine_id, project_id, "/old/root")
    clear_index_state = manager._clear_index_state

    def _abort_after_clear(conn: Any, scoped_machine_id: str, scoped_project_id: str) -> None:
        clear_index_state(conn, scoped_machine_id, scoped_project_id)
        raise RuntimeError("abort rebind")

    monkeypatch.setattr(manager, "_clear_index_state", _abort_after_clear)
    with pytest.raises(RuntimeError, match="abort rebind"):
        manager.rebind(machine_id, project_id, "/new/root")

    rolled_back = manager.get(machine_id, project_id)
    assert rolled_back is not None
    assert rolled_back.root_path == "/old/root"
    assert temp_db.fetchone(
        """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
        (machine_id, project_id),
    ) == {"root_path": "/old/root"}

    monkeypatch.setattr(manager, "_clear_index_state", clear_index_state)
    assert manager.rebind(machine_id, project_id, "/new/root").root_path == "/new/root"
    assert (
        temp_db.fetchone(
            """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
            (machine_id, project_id),
        )
        is None
    )


def test_concurrent_absent_rebind_same_root_inserts_once(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Concurrent absent-row rebind with equal roots inserts once."""
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    project_id = sample_project["id"]
    root = "/concurrent/same"
    second = PostgresHubDatabase(temp_db.conninfo)
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def _run(db: HubDatabase) -> None:
        try:
            barrier.wait(timeout=10)
            checkout = LocalProjectCheckoutManager(db).rebind(machine_id, project_id, root)
            results.append(checkout.root_path)
        except Exception as exc:
            errors.append(exc)

    try:
        threads = [
            threading.Thread(target=_run, args=(temp_db,)),
            threading.Thread(target=_run, args=(second,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert errors == []
        assert results == [root, root]
        listed = _manager(temp_db).list_for_machine(machine_id)
        assert len(listed) == 1
        assert listed[0].root_path == root
    finally:
        second.close()


def test_concurrent_absent_rebind_different_roots_serializes(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Concurrent absent-row rebind with different roots serializes to one row."""
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    project_id = sample_project["id"]
    second = PostgresHubDatabase(temp_db.conninfo)
    barrier = threading.Barrier(2)
    roots = ("/concurrent/a", "/concurrent/b")
    results: list[str] = []
    errors: list[Exception] = []

    def _run(db: HubDatabase, root: str) -> None:
        try:
            barrier.wait(timeout=10)
            checkout = LocalProjectCheckoutManager(db).rebind(machine_id, project_id, root)
            results.append(checkout.root_path)
        except Exception as exc:
            errors.append(exc)

    try:
        threads = [
            threading.Thread(target=_run, args=(temp_db, roots[0])),
            threading.Thread(target=_run, args=(second, roots[1])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert errors == []
        assert set(results) == set(roots)
        listed = _manager(temp_db).list_for_machine(machine_id)
        assert len(listed) == 1
        assert listed[0].root_path in roots
        assert listed[0].root_path in results
    finally:
        second.close()


def test_require_root_returns_checkout_root(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    assert require_root(temp_db, isolated.project.id, isolated.machine_id) == isolated.root_path


def test_require_root_raises_when_checkout_missing(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.isolated_checkout import (
        insert_isolated_machine,
        patch_local_machine_id,
    )

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project_id = sample_project_id(temp_db)
    with pytest.raises(CheckoutNotFoundError):
        require_root(temp_db, project_id, machine_id)


def sample_project_id(db: HubDatabase) -> str:
    from gobby.storage.projects import LocalProjectManager

    return LocalProjectManager(db).create(name=f"no-checkout-{uuid.uuid4().hex[:8]}").id


def test_require_root_does_not_fall_back_to_projects_repo_path(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    temp_db.execute(
        "DELETE FROM project_checkouts WHERE machine_id = %s AND project_id = %s",
        (isolated.machine_id, isolated.project.id),
    )
    with pytest.raises(CheckoutNotFoundError):
        require_root(temp_db, isolated.project.id, isolated.machine_id)


def test_require_root_refuses_missing_machine_id(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    with pytest.raises(MissingMachineContextError):
        require_root(temp_db, sample_project["id"], "")
    with pytest.raises(MissingMachineContextError):
        require_root(temp_db, sample_project["id"], None)


def test_require_root_refuses_foreign_machine_before_lookup(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    lookups: list[tuple[str, str]] = []
    original_get = LocalProjectCheckoutManager.get

    def _get(self: LocalProjectCheckoutManager, machine_id: str, project_id: str) -> Any:
        lookups.append((machine_id, project_id))
        return original_get(self, machine_id, project_id)

    monkeypatch.setattr(LocalProjectCheckoutManager, "get", _get)
    with pytest.raises(MachineOwnershipMismatchError):
        require_root(temp_db, isolated.project.id, str(uuid.uuid4()))
    assert lookups == []


@pytest.mark.parametrize("sentinel_id", sorted(CHECKOUT_FREE_PROJECT_IDS))
def test_require_root_refuses_checkout_free_sentinels(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sentinel_id: str
) -> None:
    from tests.fixtures.isolated_checkout import (
        insert_isolated_machine,
        patch_local_machine_id,
    )

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    with pytest.raises(CheckoutSentinelRejectedError):
        require_root(temp_db, sentinel_id, machine_id)


def test_resolve_operation_root_none_overlay_uses_primary(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    assert (
        resolve_operation_root(temp_db, isolated.project.id, isolated.machine_id)
        == isolated.root_path
    )
    assert (
        resolve_operation_root(temp_db, isolated.project.id, isolated.machine_id, overlay_path=None)
        == isolated.root_path
    )


def test_resolve_operation_root_none_overlay_missing_raises(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.isolated_checkout import (
        insert_isolated_machine,
        patch_local_machine_id,
    )

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project_id = sample_project_id(temp_db)
    with pytest.raises(CheckoutNotFoundError):
        resolve_operation_root(temp_db, project_id, machine_id, overlay_path=None)


def test_resolve_operation_root_registered_worktree_wins_without_primary(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.isolated_checkout import (
        insert_isolated_machine,
        insert_overlay,
        patch_local_machine_id,
    )

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project_id = sample_project_id(temp_db)
    overlay = str(tmp_path / "wt")
    insert_overlay(
        temp_db, project_id=project_id, machine_id=machine_id, path=overlay, kind="worktree"
    )
    assert resolve_operation_root(temp_db, project_id, machine_id, overlay_path=overlay) == overlay


def test_resolve_operation_root_registered_clone_wins_without_primary(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.isolated_checkout import (
        insert_isolated_machine,
        insert_overlay,
        patch_local_machine_id,
    )

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project_id = sample_project_id(temp_db)
    overlay = str(tmp_path / "clone")
    insert_overlay(
        temp_db, project_id=project_id, machine_id=machine_id, path=overlay, kind="clone"
    )
    assert resolve_operation_root(temp_db, project_id, machine_id, overlay_path=overlay) == overlay


def test_resolve_operation_root_resolves_symlinked_overlay_to_registered_path(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked overlay path resolves to the overlay registered under its realpath."""
    from tests.fixtures.isolated_checkout import (
        insert_isolated_machine,
        insert_overlay,
        patch_local_machine_id,
    )

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project_id = sample_project_id(temp_db)
    real = tmp_path / "real-wt"
    real.mkdir()
    link = tmp_path / "link-wt"
    link.symlink_to(real, target_is_directory=True)
    registered = os.path.realpath(real)
    insert_overlay(
        temp_db, project_id=project_id, machine_id=machine_id, path=registered, kind="worktree"
    )

    resolved = resolve_operation_root(temp_db, project_id, machine_id, overlay_path=str(link))

    assert resolved == registered
    assert (
        resolve_operation_root(temp_db, project_id, machine_id, overlay_path=registered)
        == registered
    )
    with pytest.raises(OverlayRegistrationRejectedError):
        resolve_operation_root(
            temp_db, project_id, machine_id, overlay_path=str(tmp_path / "unregistered-wt")
        )


def test_resolve_operation_root_refuses_unregistered_overlay(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    with pytest.raises(OverlayRegistrationRejectedError):
        resolve_operation_root(
            temp_db,
            isolated.project.id,
            isolated.machine_id,
            overlay_path=str(tmp_path / "unregistered"),
        )


def test_resolve_operation_root_refuses_wrong_project_overlay(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.isolated_checkout import (
        insert_overlay,
        install_isolated_checkout_project,
    )

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    other_id = sample_project_id(temp_db)
    overlay = str(tmp_path / "other-wt")
    insert_overlay(
        temp_db,
        project_id=other_id,
        machine_id=isolated.machine_id,
        path=overlay,
        kind="worktree",
    )
    with pytest.raises(OverlayRegistrationRejectedError):
        resolve_operation_root(
            temp_db, isolated.project.id, isolated.machine_id, overlay_path=overlay
        )


def test_resolve_operation_root_refuses_foreign_machine_overlay(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.isolated_checkout import (
        insert_isolated_machine,
        insert_overlay,
        install_isolated_checkout_project,
    )

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    foreign = insert_isolated_machine(temp_db)
    overlay = str(tmp_path / "foreign-wt")
    insert_overlay(
        temp_db,
        project_id=isolated.project.id,
        machine_id=foreign,
        path=overlay,
        kind="worktree",
    )
    with pytest.raises(OverlayRegistrationRejectedError):
        resolve_operation_root(
            temp_db, isolated.project.id, isolated.machine_id, overlay_path=overlay
        )


def test_resolve_operation_root_refuses_missing_machine_id(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    with pytest.raises(MissingMachineContextError):
        resolve_operation_root(temp_db, sample_project["id"], "")


def test_resolve_operation_root_refuses_sentinels(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.isolated_checkout import (
        insert_isolated_machine,
        patch_local_machine_id,
    )

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    with pytest.raises(CheckoutSentinelRejectedError):
        resolve_operation_root(temp_db, PERSONAL_PROJECT_ID, machine_id)


def test_resolve_operation_root_refuses_foreign_machine_before_overlay_lookup(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
    from tests.fixtures.isolated_checkout import (
        insert_overlay,
        install_isolated_checkout_project,
    )

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    overlay = str(tmp_path / "wt")
    insert_overlay(
        temp_db,
        project_id=isolated.project.id,
        machine_id=isolated.machine_id,
        path=overlay,
        kind="worktree",
    )
    queries: list[str] = []
    original_fetchone = temp_db.fetchone

    def _fetchone(sql: str, params: Any = None) -> Any:
        queries.append(sql)
        return original_fetchone(sql, params)

    monkeypatch.setattr(temp_db, "fetchone", _fetchone)
    with pytest.raises(MachineOwnershipMismatchError):
        resolve_operation_root(
            temp_db, isolated.project.id, str(uuid.uuid4()), overlay_path=overlay
        )
    assert queries == []


def test_get_and_list_remain_opaque_without_local_machine_check(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    called: list[object] = []

    def _track(*args: object, **kwargs: object) -> str:
        called.append((args, kwargs))
        return machine_id

    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_local_machine_id",
        _track,
    )
    manager = _manager(temp_db)
    created, inserted = manager.register(machine_id, sample_project["id"], "/opaque/root")
    assert inserted is True
    loaded = manager.get(machine_id, sample_project["id"])
    assert loaded == created
    assert [row.root_path for row in manager.list_for_machine(machine_id)] == ["/opaque/root"]
    assert called == []


def test_target_schema_assets_are_checkout_only() -> None:
    baseline = (_REPO_ROOT / "crates/gcore/assets/schema/baseline.sql").read_text()
    privileges = json.loads(
        (_REPO_ROOT / "crates/gcode/security/managed_postgres_privileges.json").read_text()
    )

    assert "CREATE TABLE project_checkouts (" in baseline
    assert "repo_path text" not in baseline
    assert "projects.repo_path" not in baseline
    assert "SELECT(repo_path) ON TABLE projects" not in baseline
    assert "'project-checkout-cutover'::text" in baseline
    assert "LEFT JOIN public.project_checkouts AS checkout" in baseline
    projects = next(item for item in privileges["relations"] if item["relation"] == "projects")
    assert projects["columns"] == ["id", "name", "deleted_at"]


def test_agent_spawn_resolves_machine_checkout_instead_of_logical_project_path() -> None:
    source = (Path(__file__).parents[2] / "src/gobby/servers/routes/agent_spawn.py").read_text()

    assert "project.repo_path" not in source
    assert "require_root(task_manager.db, effective_project_id, require_machine_id())" in source


def test_identity_repo_path_residue_allowlist() -> None:
    """Pin the exact gcode residue queries and their narrow historical allowlist."""
    source_roots = (
        "src/gobby",
        "crates/gcore/src",
        "crates/gcore/tests",
        "crates/gcode/src",
        "tests",
    )
    source_suffixes = {".json", ".py", ".rs", ".sql"}
    allowed_qualified_column_paths = {
        "crates/gcore/tests/schema_contract.rs",
    }
    literal_queries: tuple[tuple[str, str, set[str]], ...] = (
        (
            "gcode grep -F 'projects.repo_path' src/gobby crates/gcore/src "
            "crates/gcore/tests crates/gcode/src tests -m 500",
            "projects.repo_path",
            allowed_qualified_column_paths,
        ),
        (
            "gcode grep -F 'Project.repo_path' src/gobby crates/gcore/src "
            "crates/gcore/tests crates/gcode/src tests -m 500",
            "Project.repo_path",
            set(),
        ),
        (
            'gcode grep -F "project\'s repo_path" src/gobby -m 50',
            "project's repo_path",
            set(),
        ),
        (
            "gcode grep -F 'Project repo_path is required' src/gobby -m 50",
            "Project repo_path is required",
            set(),
        ),
        (
            "gcode grep -F 'canonical repo_path' src/gobby -m 50",
            "canonical repo_path",
            set(),
        ),
    )
    violations: dict[str, list[str]] = {}
    residue_test_path = "tests/storage/test_project_checkouts.py"
    for command, literal, allowed_paths in literal_queries:
        for root_name in source_roots:
            root = _REPO_ROOT / root_name
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in source_suffixes:
                    continue
                relative = path.relative_to(_REPO_ROOT).as_posix()
                if (
                    literal in path.read_text(errors="replace")
                    and relative not in allowed_paths
                    and relative != residue_test_path
                ):
                    violations.setdefault(command, []).append(relative)

    fixture_paths = (
        "crates/gcode/src/config/tests.rs",
        "tests/e2e/conftest.py",
        "tests/e2e/test_worktrees_e2e.py",
        "tests/integration/test_edit_history.py",
        "tests/integration/test_hub_query.py",
        "tests/mcp_proxy/test_metrics_manager.py",
        "tests/mcp_proxy/test_metrics_store.py",
        "tests/mcp_proxy/test_registries.py",
        "tests/mcp_proxy/tools/test_apply_persona.py",
        "tests/mcp_proxy/tools/test_hub.py",
        "tests/plans/test_plan_coverage_ci.py",
        "tests/sessions/test_e2e_session_tracking.py",
        "tests/sessions/test_token_usage.py",
        "tests/storage/test_checkpoints.py",
        "tests/storage/test_manager_surface_parity.py",
        "tests/storage/test_postgres_agent_authorization.py",
        "tests/storage/test_project_manager.py",
        "tests/storage/test_project_repo_path_isolation.py",
        "tests/sync/test_github_issue_sync.py",
        "tests/workflows/test_pipeline_heartbeat.py",
    )
    json_query = (
        "gcode grep -F '\"repo_path\":' crates/gcode/src/config/tests.rs "
        + " ".join(fixture_paths[1:])
        + " -m 500"
    )
    for relative in fixture_paths:
        if '"repo_path":' in (_REPO_ROOT / relative).read_text(errors="replace"):
            violations.setdefault(json_query, []).append(relative)

    positional_query = (
        "gcode grep '\\.(create|ensure_exists|update)\\(' "
        + " ".join(path for path in fixture_paths if path.endswith(".py"))
        + " -m 500"
    )
    positional_limits = {"create": 2, "ensure_exists": 3, "update": 2}
    manager_names = {"pm", "project_manager", "projects"}
    for relative in (path for path in fixture_paths if path.endswith(".py")):
        tree = ast.parse((_REPO_ROOT / relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                    if value.func.id == "LocalProjectManager":
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        manager_names.update(
                            target.id for target in targets if isinstance(target, ast.Name)
                        )
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            limit = positional_limits.get(node.func.attr)
            if limit is None or len(node.args) < limit:
                continue
            owner = node.func.value
            direct_manager = (
                isinstance(owner, ast.Call)
                and isinstance(owner.func, ast.Name)
                and owner.func.id == "LocalProjectManager"
            )
            named_manager = isinstance(owner, ast.Name) and owner.id in manager_names
            if direct_manager or named_manager:
                violations.setdefault(positional_query, []).append(f"{relative}:{node.lineno}")

    privileges = json.loads(
        (_REPO_ROOT / "crates/gcode/security/managed_postgres_privileges.json").read_text()
    )
    projects = next(item for item in privileges["relations"] if item["relation"] == "projects")
    if projects["columns"] != ["id", "name", "deleted_at"]:
        violations.setdefault(
            'gcode grep -F \'"relation": "projects"\' '
            "crates/gcode/security/managed_postgres_privileges.json -A 12 -m 20",
            [],
        ).append(str(projects["columns"]))

    assert not violations, json.dumps(violations, indent=2, sort_keys=True)
