"""Tests for LocalProjectCheckoutManager and checkout-free sentinel IDs."""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import (
    CheckoutConflictError,
    CheckoutRootTakenError,
    CheckoutSentinelRejectedError,
    LocalProjectCheckoutManager,
    OverlayRegistrationRejectedError,
)
from gobby.storage.projects import (
    CHECKOUT_FREE_PROJECT_IDS,
    GLOBAL_PROJECT_ID,
    MIGRATED_PROJECT_ID,
    ORPHANED_PROJECT_ID,
    PERSONAL_PROJECT_ID,
)
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.integration


def test_checkout_free_project_ids_pin_the_four_sentinels() -> None:
    assert MIGRATED_PROJECT_ID == "00000000-0000-0000-0000-000000000001"
    assert CHECKOUT_FREE_PROJECT_IDS == frozenset(
        {
            ORPHANED_PROJECT_ID,
            MIGRATED_PROJECT_ID,
            GLOBAL_PROJECT_ID,
            PERSONAL_PROJECT_ID,
        }
    )


def _insert_machine(db: HubDatabase, machine_id: str) -> None:
    db.execute(
        "INSERT INTO machines (id, hostname, owner_user_id) VALUES (%s, %s, %s)",
        (machine_id, f"host-{machine_id}", TEST_USER_ID),
    )


def _manager(db: HubDatabase) -> LocalProjectCheckoutManager:
    return LocalProjectCheckoutManager(db)


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


@pytest.mark.parametrize("sentinel_id", sorted(CHECKOUT_FREE_PROJECT_IDS))
def test_register_and_rebind_refuse_checkout_free_sentinels(
    temp_db: HubDatabase, sentinel_id: str
) -> None:
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    manager = _manager(temp_db)
    with pytest.raises(CheckoutSentinelRejectedError):
        manager.register(machine_id, sentinel_id, "/sentinel/root")
    with pytest.raises(CheckoutSentinelRejectedError):
        manager.rebind(machine_id, sentinel_id, "/sentinel/root")


def test_rebind_inserts_noops_and_updates_roots(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
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
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    manager = _manager(temp_db)
    project_id = sample_project["id"]
    worktree_path = "/tmp/wt-overlay"
    clone_path = "/tmp/clone-overlay"
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


def test_rebind_insert_leaves_index_state_for_later_cleanup(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    machine_id = str(uuid.uuid4())
    _insert_machine(temp_db, machine_id)
    project_id = sample_project["id"]
    temp_db.execute(
        "INSERT INTO code_indexed_projects (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
        (project_id,),
    )
    temp_db.execute(
        """
        INSERT INTO code_indexed_project_states (
            machine_id, project_id, root_path, total_files, total_symbols
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (machine_id, project_id, "/old/index-root", 3, 9),
    )
    manager = _manager(temp_db)
    manager.rebind(machine_id, project_id, "/new/checkout-root")
    state = temp_db.fetchone(
        """
        SELECT root_path FROM code_indexed_project_states
        WHERE machine_id = %s AND project_id = %s
        """,
        (machine_id, project_id),
    )
    assert state is not None
    assert state["root_path"] == "/old/index-root"


def test_concurrent_absent_rebind_same_root_inserts_once(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
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
        assert set(results) <= set(roots)
        listed = _manager(temp_db).list_for_machine(machine_id)
        assert len(listed) == 1
        assert listed[0].root_path in roots
    finally:
        second.close()
