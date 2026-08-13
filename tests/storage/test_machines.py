"""Tests for machine registry storage."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.identity import hash_password
from gobby.runner_init.helpers import ensure_machine_identity
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import (
    LocalMachineManager,
    Machine,
    MachineNotRegisteredError,
    MachineOwnershipConflictError,
)
from gobby.storage.sessions import SessionManager
from gobby.storage.users import LocalUserManager, UserIdentityStateError
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID

pytestmark = pytest.mark.unit

MACHINE_A = "8fa1247f-e924-4bd7-a54e-b9dd5704304a"
MACHINE_B = "54ba70ce-3ec4-470d-905a-dcb40704abfd"
OTHER_USER_ID = "20000000-0000-4000-8000-000000000002"


def _count_machines(temp_db: HubDatabase) -> int:
    row = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM machines WHERE id::TEXT NOT LIKE %s",
        (f"{TEST_MACHINE_ID_PREFIX}%",),
    )
    return int(row["count"]) if row else 0


class TestLocalMachineManager:
    def test_machine_from_row_rejects_missing_owner(self, temp_db: HubDatabase) -> None:
        row = temp_db.fetchone("SELECT * FROM machines ORDER BY id LIMIT 1")
        assert row is not None

        with pytest.raises(ValueError, match="missing owner_user_id"):
            Machine.from_row({**row, "owner_user_id": None})

    def test_upsert_seen_inserts_and_refreshes_last_seen(self, temp_db: HubDatabase) -> None:
        manager = LocalMachineManager(temp_db)

        first = manager.upsert_seen(
            MACHINE_A,
            TEST_USER_ID,
            hostname="host-a",
            os="Darwin",
            seen_at="2026-01-01T00:00:00+00:00",
        )
        refreshed = manager.upsert_seen(
            MACHINE_A,
            TEST_USER_ID,
            os="Linux",
            label="laptop",
            seen_at="2026-01-02T00:00:00+00:00",
        )

        assert first is not None
        assert refreshed is not None
        assert refreshed.id == MACHINE_A
        assert refreshed.hostname == "host-a"
        assert refreshed.os == "Linux"
        assert refreshed.label == "laptop"
        assert refreshed.first_seen == first.first_seen
        assert str(refreshed.last_seen).startswith("2026-01-02")

    def test_refresh_seen_does_not_create_unknown_machine(self, temp_db: HubDatabase) -> None:
        manager = LocalMachineManager(temp_db)
        before = _count_machines(temp_db)

        assert manager.refresh_seen(MACHINE_A) is None

        assert _count_machines(temp_db) == before

    def test_manager_canonicalizes_uuid_and_rejects_non_uuid(self, temp_db: HubDatabase) -> None:
        manager = LocalMachineManager(temp_db)
        manager.upsert_seen(MACHINE_A, TEST_USER_ID)

        assert manager.get(f" {MACHINE_A} ") is not None
        with pytest.raises(ValueError, match="badly formed hexadecimal UUID"):
            manager.get("unknown-machine")
        with pytest.raises(ValueError, match="badly formed hexadecimal UUID"):
            manager.upsert_seen("unknown-machine", TEST_USER_ID)

    def test_upsert_seen_throttles_last_seen_refresh(self, temp_db: HubDatabase) -> None:
        manager = LocalMachineManager(temp_db)
        first = manager.upsert_seen(MACHINE_A, TEST_USER_ID, seen_at="2026-01-01T00:00:00+00:00")
        throttled = manager.refresh_seen(MACHINE_A, seen_at="2026-01-01T00:01:00+00:00")

        assert first is not None
        assert throttled is not None
        assert throttled.last_seen == first.last_seen

    def test_upsert_seen_rejects_conflicting_owner_without_mutation(self, temp_db: HubDatabase) -> None:
        users = LocalUserManager(temp_db)
        users.create(
            user_id=OTHER_USER_ID,
            name="Other User",
            email="other-owner@example.com",
            password_hash=hash_password("password"),
        )
        manager = LocalMachineManager(temp_db)
        original = manager.upsert_seen(MACHINE_A, TEST_USER_ID, label="original")

        with pytest.raises(MachineOwnershipConflictError) as raised:
            manager.upsert_seen(MACHINE_A, OTHER_USER_ID, label="changed")

        assert raised.value.owner_user_id == TEST_USER_ID
        assert manager.get(MACHINE_A) == original

    def test_list_for_user_returns_owned_machines(self, temp_db: HubDatabase) -> None:
        manager = LocalMachineManager(temp_db)
        users = LocalUserManager(temp_db)
        users.create(
            user_id=OTHER_USER_ID,
            name="Other User",
            email="other-list-owner@example.com",
            password_hash=hash_password("password"),
        )
        manager.upsert_seen(MACHINE_A, TEST_USER_ID)
        manager.upsert_seen(MACHINE_B, OTHER_USER_ID)

        machine_ids = {machine.id for machine in manager.list_for_user(TEST_USER_ID)}

        assert MACHINE_A in machine_ids
        assert MACHINE_B not in machine_ids


def test_session_registration_refreshes_registered_machine(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    local_machine_id = require_machine_id()
    before = LocalMachineManager(session_manager.db).get(local_machine_id)
    assert before is not None
    session_manager.register(
        external_id="session-machine-registration",
        machine_id=local_machine_id,
        source="claude",
        project_id=sample_project["id"],
    )

    machine = LocalMachineManager(session_manager.db).get(local_machine_id)
    assert machine is not None
    assert machine.id == local_machine_id
    assert machine.owner_user_id == before.owner_user_id


def test_session_registration_rejects_unknown_local_machine(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id",
        lambda: MACHINE_A,
    )

    with pytest.raises(MachineNotRegisteredError, match="authenticated enrollment"):
        session_manager.register(
            external_id="session-machine-registration-unknown",
            machine_id=MACHINE_A,
            source="claude",
            project_id=sample_project["id"],
        )

    assert LocalMachineManager(session_manager.db).get(MACHINE_A) is None
    assert (
        session_manager.find_by_external_id(
            "session-machine-registration-unknown",
            sample_project["id"],
            "claude",
        )
        is None
    )


def test_session_registration_rejects_foreign_machine(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    with pytest.raises(MachineOwnershipMismatchError):
        session_manager.register(
            external_id="session-machine-registration-foreign",
            machine_id=MACHINE_B,
            source="claude",
            project_id=sample_project["id"],
        )


def test_fresh_boot_registers_identity(temp_db: HubDatabase) -> None:
    registered_id = ensure_machine_identity(temp_db, MACHINE_A)

    assert registered_id == MACHINE_A
    machine = LocalMachineManager(temp_db).get(MACHINE_A)
    assert machine is not None
    assert machine.owner_user_id == TEST_USER_ID


def test_fresh_boot_refuses_to_register_machine_without_canonical_user(
    temp_db: HubDatabase,
) -> None:
    temp_db.execute("DELETE FROM machines")
    temp_db.execute("DELETE FROM users")

    with pytest.raises(UserIdentityStateError, match="exactly one installed user, found 0"):
        ensure_machine_identity(temp_db, MACHINE_A)

    assert LocalMachineManager(temp_db).get(MACHINE_A) is None
