"""Focused coverage for session usage guards, bootstrap logging, and write transactions."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager, ensure_system_session
from gobby.storage.sessions._constants import system_session_external_id, system_session_id
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError


@pytest.fixture(autouse=True)
def _local_machine_identity(temp_db: HubDatabase) -> Iterator[None]:
    LocalMachineManager(temp_db).upsert_seen("20000000-0000-4000-8000-000000000002")
    with patch(
        "gobby.utils.machine_id.get_machine_id",
        return_value="20000000-0000-4000-8000-000000000002",
    ):
        yield


def test_system_session_bootstrap_creates_machine_scoped_rows(temp_db: HubDatabase) -> None:
    first_machine = "20000000-0000-4000-8000-000000000001"
    later_machine = "20000000-0000-4000-8000-000000000002"
    machines = LocalMachineManager(temp_db)
    machines.upsert_seen(first_machine)
    machines.upsert_seen(later_machine)
    first_id = system_session_id(first_machine)
    later_id = system_session_id(later_machine)

    with patch("gobby.utils.machine_id.get_machine_id", return_value=first_machine):
        ensure_system_session(temp_db)

    with patch("gobby.utils.machine_id.get_machine_id", return_value=later_machine):
        ensure_system_session(temp_db)

    rows = temp_db.fetchall(
        "SELECT id, machine_id, external_id FROM sessions WHERE id IN (%s, %s)",
        (first_id, later_id),
    )
    by_id = {str(row["id"]): row for row in rows}
    assert first_id != later_id
    assert str(by_id[first_id]["machine_id"]) == first_machine
    assert by_id[first_id]["external_id"] == system_session_external_id(first_machine)
    assert str(by_id[later_id]["machine_id"]) == later_machine
    assert by_id[later_id]["external_id"] == system_session_external_id(later_machine)


def test_system_session_bootstrap_rejects_wrong_owner(temp_db: HubDatabase) -> None:
    local_machine = "20000000-0000-4000-8000-000000000001"
    foreign_machine = "20000000-0000-4000-8000-000000000002"
    machines = LocalMachineManager(temp_db)
    machines.upsert_seen(local_machine)
    machines.upsert_seen(foreign_machine)

    with patch("gobby.utils.machine_id.get_machine_id", return_value=local_machine):
        ensure_system_session(temp_db)
    temp_db.execute(
        "UPDATE sessions SET machine_id = %s WHERE id = %s",
        (foreign_machine, system_session_id(local_machine)),
    )

    with (
        patch("gobby.utils.machine_id.get_machine_id", return_value=local_machine),
        pytest.raises(MachineOwnershipMismatchError),
    ):
        ensure_system_session(temp_db)


pytestmark = pytest.mark.unit


def _register_session(session_manager: SessionManager, project_id: str) -> str:
    session = session_manager.register(
        external_id="usage-session",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="claude",
        project_id=project_id,
    )
    return session.id


def test_update_usage_rejects_negative_absolute_counts(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    session_id = _register_session(session_manager, sample_project["id"])
    assert session_manager.update_usage(session_id, 5, 4, 3, 2) is True

    result = session_manager.update_usage(session_id, -1, 4, 3, 2)

    assert result is False
    session = session_manager.get(session_id)
    assert session is not None
    assert session.usage_input_tokens == 5
    assert session.usage_output_tokens == 4
    assert session.usage_cache_creation_tokens == 3
    assert session.usage_cache_read_tokens == 2


def test_add_usage_delta_clamps_counters_to_zero(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    session_id = _register_session(session_manager, sample_project["id"])
    assert session_manager.update_usage(session_id, 5, 4, 3, 2) is True

    result = session_manager.add_usage_delta(
        session_id,
        input_tokens=-50,
        output_tokens=-40,
        cache_creation_tokens=-30,
        cache_read_tokens=-20,
    )

    assert result is True
    session = session_manager.get(session_id)
    assert session is not None
    assert session.usage_input_tokens == 0
    assert session.usage_output_tokens == 0
    assert session.usage_cache_creation_tokens == 0
    assert session.usage_cache_read_tokens == 0


def test_ensure_system_session_logs_first_create_at_info(temp_db: HubDatabase) -> None:
    temp_db.execute("DELETE FROM sessions")

    with patch("gobby.storage.sessions.logger") as mock_logger:
        ensure_system_session(temp_db)

    mock_logger.info.assert_called_once_with("Created system session %s", system_session_id())
    assert mock_logger.info.call_count == 1
    assert mock_logger.info.call_args is not None
    mock_logger.warning.assert_not_called()
    assert mock_logger.warning.call_count == 0
    assert not mock_logger.warning.called


def test_ensure_system_session_logs_recreation_at_warning(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    session_manager.register(
        external_id="other-session",
        machine_id="20000000-0000-4000-8000-000000000002",
        source="claude",
        project_id=sample_project["id"],
    )
    session_manager.db.execute("DELETE FROM sessions WHERE id = %s", (system_session_id(),))

    with patch("gobby.storage.sessions.logger") as mock_logger:
        ensure_system_session(session_manager.db)

    mock_logger.warning.assert_called_once_with(
        "Recreated missing system session %s",
        system_session_id(),
    )
    assert mock_logger.warning.call_count == 1
    assert mock_logger.warning.call_args is not None


def test_bulk_update_wraps_safe_update_in_transaction(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    session_id = _register_session(session_manager, sample_project["id"])

    with patch.object(
        session_manager.db, "transaction", wraps=session_manager.db.transaction
    ) as txn:
        session_manager.update(session_id, title="Updated title")

    txn.assert_called()
    assert txn.call_args is not None


def test_touch_wraps_write_in_transaction(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    session_id = _register_session(session_manager, sample_project["id"])

    with patch.object(
        session_manager.db, "transaction", wraps=session_manager.db.transaction
    ) as txn:
        session_manager.touch(session_id)

    txn.assert_called_once()
    assert txn.call_count == 1
    assert txn.call_args is not None


def test_update_summary_wraps_write_in_transaction(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    session_id = _register_session(session_manager, sample_project["id"])

    with patch.object(
        session_manager.db, "transaction", wraps=session_manager.db.transaction
    ) as txn:
        session_manager.update_summary(session_id, summary_markdown="Summary")

    txn.assert_called()
    assert txn.call_args is not None
