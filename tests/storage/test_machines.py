"""Tests for machine registry storage."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.runner_init.helpers import ensure_machine_identity
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager, normalize_machine_id
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

MACHINE_A = "8fa1247f-e924-4bd7-a54e-b9dd5704304a"
MACHINE_B = "54ba70ce-3ec4-470d-905a-dcb40704abfd"


def _count_machines(temp_db: HubDatabase) -> int:
    row = temp_db.fetchone("SELECT COUNT(*) AS count FROM machines")
    return int(row["count"]) if row else 0


class TestNormalizeMachineId:
    def test_skips_missing_and_placeholder_ids(self) -> None:
        for value in (
            None,
            "",
            "   ",
            "unknown",
            "unknown-machine",
            "UNKNOWN",
        ):
            assert normalize_machine_id(value) is None

    def test_canonicalizes_uuid(self) -> None:
        assert normalize_machine_id(f" {MACHINE_A.upper()} ") == MACHINE_A

    def test_rejects_non_uuid_id(self) -> None:
        with pytest.raises(ValueError, match="valid UUID"):
            normalize_machine_id("machine-abc")


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

    def test_upsert_seen_skips_placeholder_ids(self, temp_db) -> None:
        manager = LocalMachineManager(temp_db)

        assert manager.upsert_seen("unknown-machine") is None
        assert manager.upsert_seen("   ") is None

        assert _count_machines(temp_db) == 0

    def test_get_normalizes_and_rejects_placeholder_ids(self, temp_db) -> None:
        manager = LocalMachineManager(temp_db)
        manager.upsert_seen(MACHINE_A)

        assert manager.get(f" {MACHINE_A} ") is not None
        assert manager.get("unknown-machine") is None

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
    session_manager.register(
        external_id="session-machine-registration",
        machine_id=MACHINE_B,
        source="claude",
        project_id=sample_project["id"],
    )

    machine = LocalMachineManager(session_manager.db).get(MACHINE_B)
    assert machine is not None
    assert machine.id == MACHINE_B


def test_fresh_boot_registers_identity(temp_db: HubDatabase, tmp_path: Path) -> None:
    identity_file = tmp_path / "machine_id"
    identity_file.write_text(MACHINE_A)

    registered_id = ensure_machine_identity(temp_db, MACHINE_A, identity_file=identity_file)

    assert registered_id == MACHINE_A
    assert LocalMachineManager(temp_db).get(MACHINE_A) is not None


def test_tombstoned_boot_identity_rekeys_and_registers(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    identity_file = tmp_path / "machine_id"
    identity_file.write_text(MACHINE_A)
    temp_db.execute(
        """
        INSERT INTO retired_machine_identities(old_id, disposition)
        VALUES (%s, 'identity-cutover-retired')
        """,
        (MACHINE_A,),
    )

    with patch("gobby.runner_init.helpers._generate_machine_id", return_value=MACHINE_B):
        registered_id = ensure_machine_identity(
            temp_db,
            MACHINE_A,
            identity_file=identity_file,
        )

    assert registered_id == MACHINE_B
    assert identity_file.read_text() == MACHINE_B
    assert LocalMachineManager(temp_db).get(MACHINE_B) is not None
