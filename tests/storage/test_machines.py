"""Tests for machine registry storage."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.runner_init.helpers import ensure_machine_identity
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX

pytestmark = pytest.mark.unit

MACHINE_A = "8fa1247f-e924-4bd7-a54e-b9dd5704304a"
MACHINE_B = "54ba70ce-3ec4-470d-905a-dcb40704abfd"


def _count_machines(temp_db: HubDatabase) -> int:
    row = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM machines WHERE id::TEXT NOT LIKE %s",
        (f"{TEST_MACHINE_ID_PREFIX}%",),
    )
    return int(row["count"]) if row else 0


class TestLocalMachineManager:
    def test_upsert_seen_inserts_and_refreshes_last_seen(self, temp_db) -> None:
        manager = LocalMachineManager(temp_db)

        first = manager.upsert_seen(
            MACHINE_A,
            hostname="host-a",
            os="Darwin",
            seen_at="2026-01-01T00:00:00+00:00",
        )
        refreshed = manager.upsert_seen(
            MACHINE_A,
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

    def test_upsert_seen_skips_missing_attribution(self, temp_db) -> None:
        manager = LocalMachineManager(temp_db)
        before = _count_machines(temp_db)

        assert manager.upsert_seen(None) is None

        assert _count_machines(temp_db) == before

    def test_manager_canonicalizes_uuid_and_rejects_non_uuid(self, temp_db) -> None:
        manager = LocalMachineManager(temp_db)
        manager.upsert_seen(MACHINE_A)

        assert manager.get(f" {MACHINE_A} ") is not None
        with pytest.raises(ValueError, match="badly formed hexadecimal UUID"):
            manager.get("unknown-machine")
        with pytest.raises(ValueError, match="badly formed hexadecimal UUID"):
            manager.upsert_seen("unknown-machine")

    def test_upsert_seen_throttles_last_seen_refresh(self, temp_db) -> None:
        manager = LocalMachineManager(temp_db)
        first = manager.upsert_seen(MACHINE_A, seen_at="2026-01-01T00:00:00+00:00")
        throttled = manager.upsert_seen(MACHINE_A, seen_at="2026-01-01T00:01:00+00:00")

        assert first is not None
        assert throttled is not None
        assert throttled.last_seen == first.last_seen


def test_session_registration_upserts_machine(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    # Registration is machine-scoped: an explicit foreign id is rejected, so the
    # registry row this upserts is always the current machine's.
    local_machine_id = require_machine_id()
    session_manager.register(
        external_id="session-machine-registration",
        machine_id=local_machine_id,
        source="claude",
        project_id=sample_project["id"],
    )

    machine = LocalMachineManager(session_manager.db).get(local_machine_id)
    assert machine is not None
    assert machine.id == local_machine_id


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
    assert LocalMachineManager(temp_db).get(MACHINE_A) is not None
