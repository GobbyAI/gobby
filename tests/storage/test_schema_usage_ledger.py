"""Usage ledger schema: claim intervals, reported runs, api_calls and quota details (#23597)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.schema_contract import expected_schema_identity
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import get_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

USAGE_LEDGER_VERSION = 463


def _machine_id(db: HubDatabase) -> str:
    machine_id = get_machine_id()
    assert machine_id is not None
    LocalMachineManager(db).upsert_seen(machine_id, TEST_USER_ID)
    return machine_id


def _session(session_manager: SessionManager, project: dict[str, Any], name: str) -> str:
    return session_manager.register(
        external_id=f"usage-ledger-{name}",
        machine_id=_machine_id(session_manager.db),
        source="claude",
        project_id=project["id"],
    ).id


def _task(db: HubDatabase, project: dict[str, Any]) -> str:
    return (
        LocalTaskManager(db)
        .create_task(project["id"], "Claim interval", validation_criteria="Intervals follow.")
        .id
    )


def _execute(db: HubDatabase, sql: str, params: tuple[Any, ...]) -> None:
    with db.transaction() as conn:
        conn.execute(sql, params)


def _set_holder(db: HubDatabase, task_id: str, session_id: str | None) -> None:
    _execute(db, "UPDATE tasks SET claimed_by_session_id = %s WHERE id = %s", (session_id, task_id))


def _set_closed(db: HubDatabase, task_id: str, closed_at: datetime | None) -> None:
    _execute(db, "UPDATE tasks SET closed_at = %s WHERE id = %s", (closed_at, task_id))


def _intervals(db: HubDatabase, task_id: str) -> list[dict[str, Any]]:
    with db.transaction() as conn:
        rows = conn.execute(
            """
            SELECT id::text AS id, session_id::text AS session_id, claimed_at, released_at
              FROM task_claim_intervals
             WHERE task_id = %s
             ORDER BY claimed_at, id
            """,
            (task_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_trigger_tracks_holder_changes(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    first = _session(session_manager, sample_project, "first")
    second = _session(session_manager, sample_project, "second")
    task_id = _task(temp_db, sample_project)

    _set_holder(temp_db, task_id, first)
    _set_holder(temp_db, task_id, second)
    _set_holder(temp_db, task_id, None)

    intervals = _intervals(temp_db, task_id)
    assert [interval["session_id"] for interval in intervals] == [first, second]
    assert all(
        interval["released_at"] is not None and interval["released_at"] >= interval["claimed_at"]
        for interval in intervals
    )


def test_close_and_reopen_follow_holder(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    holder = _session(session_manager, sample_project, "holder")
    task_id = _task(temp_db, sample_project)

    _set_holder(temp_db, task_id, holder)
    _set_closed(temp_db, task_id, datetime.now(UTC))
    closed = _intervals(temp_db, task_id)
    _set_closed(temp_db, task_id, None)
    reopened = _intervals(temp_db, task_id)

    assert [interval["released_at"] is not None for interval in closed] == [True]
    assert [interval["session_id"] for interval in reopened] == [holder, holder]
    assert reopened[1]["released_at"] is None
    with pytest.raises(psycopg.errors.UniqueViolation):
        _execute(
            temp_db,
            "INSERT INTO task_claim_intervals (task_id, session_id, claimed_at) "
            "VALUES (%s, %s, clock_timestamp())",
            (task_id, holder),
        )


def test_task_history_index_orders_by_claim(temp_db: HubDatabase) -> None:
    with temp_db.transaction() as conn:
        row = conn.execute(
            "SELECT tablename, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = %s",
            ("idx_task_claim_intervals_task",),
        ).fetchone()
    assert row is not None
    assert row["tablename"] == "task_claim_intervals"
    assert row["indexdef"].endswith("USING btree (task_id, claimed_at)")


def test_sync_seeds_current_holder_idempotently(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    holder = _session(session_manager, sample_project, "seeded")
    task_id = _task(temp_db, sample_project)
    _set_holder(temp_db, task_id, holder)
    _execute(temp_db, "DELETE FROM task_claim_intervals WHERE task_id = %s", (task_id,))

    _execute(temp_db, "SELECT sync_task_claim_interval(%s)", (task_id,))
    seeded = _intervals(temp_db, task_id)
    _execute(temp_db, "SELECT sync_task_claim_interval(%s)", (task_id,))

    assert [(row["session_id"], row["released_at"]) for row in seeded] == [(holder, None)]
    assert _intervals(temp_db, task_id) == seeded


def test_ledger_columns_constrain_values(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    session_id = _session(session_manager, sample_project, "constrained")
    task_id = _task(temp_db, sample_project)
    _set_holder(temp_db, task_id, session_id)
    _set_holder(temp_db, task_id, None)
    started = datetime.now(UTC)
    report = (
        "INSERT INTO session_reported_usage (session_id, run_key, source, cost_unit, "
        "cost_amount, started_at, observed_at) VALUES (%s, %s, 'droid', %s, %s, %s, %s)"
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        _execute(
            temp_db,
            "INSERT INTO token_events (session_id, source, origin, event_at, api_calls) "
            "VALUES (%s, 'claude', 'transcript', now(), -1)",
            (session_id,),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        _execute(temp_db, report, (session_id, "unit", "tokens", 1, started, started))
    with pytest.raises(psycopg.errors.CheckViolation):
        _execute(temp_db, report, (session_id, "amount", "usd", None, started, started))
    with pytest.raises(psycopg.errors.CheckViolation):
        _execute(
            temp_db, report, (session_id, "order", None, None, started, started - timedelta(1))
        )
    _execute(temp_db, report, (session_id, "kept", "usd", 0.5, started, started))
    _execute(temp_db, "DELETE FROM sessions WHERE id = %s", (session_id,))

    with temp_db.transaction() as conn:
        reported = conn.execute(
            "SELECT count(*) AS n FROM session_reported_usage WHERE session_id = %s",
            (session_id,),
        ).fetchone()
    assert reported is not None
    assert reported["n"] == 0
    assert _intervals(temp_db, task_id) == []


def test_capacity_details_default_and_identity(temp_db: HubDatabase) -> None:
    machine_id = _machine_id(temp_db)
    _execute(
        temp_db,
        "INSERT INTO provider_capacity_snapshots (machine_id, provider, state, observed_at, "
        "windows, source_version) VALUES (%s, 'codex', 'available', now(), '[]', 'test')",
        (machine_id,),
    )

    with temp_db.transaction() as conn:
        row = conn.execute(
            "SELECT details FROM provider_capacity_snapshots "
            "WHERE machine_id = %s AND provider = 'codex'",
            (machine_id,),
        ).fetchone()
    assert row is not None
    assert json.loads(row["details"]) == {}
    assert expected_schema_identity()["latest_version"] == USAGE_LEDGER_VERSION
