"""Tests for machine registry storage."""

from __future__ import annotations

import pytest

from gobby.storage.machines import LocalMachineManager, normalize_machine_id
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


def _count_machines(temp_db) -> int:
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
            "legacy-missing:00000000-0000-0000-0000-000000000000",
        ):
            assert normalize_machine_id(value) is None

    def test_trims_real_id(self) -> None:
        assert normalize_machine_id(" machine-abc ") == "machine-abc"


class TestLocalMachineManager:
    def test_upsert_seen_inserts_and_refreshes_last_seen(self, temp_db) -> None:
        manager = LocalMachineManager(temp_db)

        first = manager.upsert_seen(
            "machine-a",
            hostname="host-a",
            os="Darwin",
            seen_at="2026-01-01T00:00:00+00:00",
        )
        refreshed = manager.upsert_seen(
            "machine-a",
            os="Linux",
            label="laptop",
            seen_at="2026-01-02T00:00:00+00:00",
        )

        assert first is not None
        assert refreshed is not None
        assert refreshed.machine_id == "machine-a"
        assert refreshed.hostname == "host-a"
        assert refreshed.os == "Linux"
        assert refreshed.label == "laptop"
        assert refreshed.first_seen == first.first_seen
        assert str(refreshed.last_seen).startswith("2026-01-02")

    def test_upsert_seen_skips_placeholder_ids(self, temp_db) -> None:
        manager = LocalMachineManager(temp_db)

        assert manager.upsert_seen("unknown-machine") is None
        assert manager.upsert_seen("legacy-missing:00000000-0000-0000-0000-000000000000") is None
        assert manager.upsert_seen("   ") is None

        assert _count_machines(temp_db) == 0


def test_session_registration_upserts_machine(
    session_manager: SessionManager, sample_project
) -> None:
    session_manager.register(
        external_id="session-machine-registration",
        machine_id="machine-session",
        source="claude",
        project_id=sample_project["id"],
    )

    machine = LocalMachineManager(session_manager.db).get("machine-session")
    assert machine is not None
    assert machine.machine_id == "machine-session"
