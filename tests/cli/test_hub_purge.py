from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import click
import psycopg
import pytest
from psycopg.rows import dict_row

import gobby.cli.hub_purge as hub_purge
from gobby.cli.hub_maintenance import _load_campaign_executor
from gobby.cli.hub_purge import _PurgeExecutor
from gobby.storage.maintenance_epoch import DestructiveBatch, MaintenanceEpoch
from tests.fixtures.postgres import isolated_test_schema

TARGETS = (
    "metrics_events",
    "session_variables",
    "token_events",
    "loop_progress",
    "step_executions",
    "spans",
)


@dataclass(frozen=True)
class _PurgeState:
    metrics_allow: int
    metrics_other: int
    expired_variables: int
    variables_total: int
    token_events: int
    loop_progress: int
    completed_payloads: int
    running_payloads: int
    step_rows: int
    spans: int


@pytest.fixture
def purge_database(postgres_database_url: str) -> Iterator[str]:
    with isolated_test_schema(postgres_database_url, "purge") as schema:
        scoped_dsn = postgres_database_url + f"?options=-csearch_path%3D{schema}"
        _seed_purge_tables(scoped_dsn)
        yield scoped_dsn


def test_purge_executor_is_registered() -> None:
    assert isinstance(_load_campaign_executor("purge"), _PurgeExecutor)


@pytest.mark.integration
def test_purge_executor_reclaims_six_categories_outside_vacuum_transactions(
    purge_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real_connect = hub_purge._connect
    connection_modes: list[tuple[str, bool]] = []

    def tracked_connect(
        database_url: str,
        *,
        autocommit: bool,
        application_name: str,
    ) -> psycopg.Connection[dict[str, object]]:
        connection_modes.append((application_name, autocommit))
        return real_connect(
            database_url,
            autocommit=autocommit,
            application_name=application_name,
        )

    monkeypatch.setattr(hub_purge, "_connect", tracked_connect)
    monkeypatch.setattr(hub_purge, "_assert_quiescent", lambda _connection: None)
    epoch, batch = _campaign_records()
    executor = _PurgeExecutor(database_url=purge_database, disk_path=tmp_path)
    caplog.set_level(logging.INFO, logger=hub_purge.__name__)

    executor.apply(epoch, batch)
    first_state = _purge_state(purge_database)
    executor.apply(epoch, batch)
    second_state = _purge_state(purge_database)
    executor.verify(epoch, batch)

    assert first_state == second_state
    assert second_state == _PurgeState(
        metrics_allow=0,
        metrics_other=1,
        expired_variables=0,
        variables_total=3,
        token_events=0,
        loop_progress=0,
        completed_payloads=0,
        running_payloads=1,
        step_rows=2,
        spans=0,
    )
    for target in TARGETS:
        dml_modes = [
            mode for name, mode in connection_modes if name == f"gobby-hub-purge-dml:{target}"
        ]
        vacuum_modes = [
            mode for name, mode in connection_modes if name == f"gobby-hub-purge-vacuum:{target}"
        ]
        assert dml_modes == [False, False]
        assert vacuum_modes == [True, True]
    assert sum("purge DML ledger" in record.message for record in caplog.records) == 12
    assert sum("purge VACUUM ledger" in record.message for record in caplog.records) == 12
    assert sum("purge preflight" in record.message for record in caplog.records) == 2


@pytest.mark.integration
def test_purge_preflight_refuses_another_database_connection(
    postgres_database_url: str,
) -> None:
    with (
        psycopg.connect(
            postgres_database_url,
            autocommit=True,
            application_name="gobby-purge-test-blocker",
            row_factory=dict_row,
        ),
        psycopg.connect(
            postgres_database_url,
            autocommit=True,
            application_name="gobby-purge-test-preflight",
            row_factory=dict_row,
        ) as connection,
        pytest.raises(click.ClickException, match="other PostgreSQL connection"),
    ):
        hub_purge._assert_quiescent(connection)


def _campaign_records() -> tuple[MaintenanceEpoch, DestructiveBatch]:
    now = datetime.now(UTC)
    epoch = MaintenanceEpoch(
        id=uuid.uuid4(),
        campaign="purge",
        opened_at=now,
        opened_by="hub-maintenance:purge",
        scope_note="purge integration test",
        released_at=None,
        released_by_command=None,
    )
    batch = DestructiveBatch(
        id=uuid.uuid4(),
        maintenance_epoch_id=epoch.id,
        campaign="purge",
        status="pending",
        backup_manifest_path="test-manifest.json",
        backup_manifest_sha256="a" * 64,
        intent={"campaign": "purge"},
        migration_plan=[],
        target_receipts={},
        created_at=now,
        updated_at=now,
        verified_at=None,
        aborted_at=None,
        abort_disposition=None,
    )
    return epoch, batch


def _seed_purge_tables(database_url: str) -> None:
    statements = (
        "CREATE TABLE metrics_events (event_type TEXT NOT NULL, result TEXT)",
        "CREATE TABLE sessions (id UUID PRIMARY KEY, status TEXT, updated_at TIMESTAMPTZ)",
        "CREATE TABLE session_variables (session_id UUID NOT NULL, name TEXT NOT NULL)",
        "CREATE TABLE token_events (id INTEGER GENERATED ALWAYS AS IDENTITY)",
        "CREATE TABLE loop_progress (id INTEGER GENERATED ALWAYS AS IDENTITY)",
        "CREATE TABLE step_executions (status TEXT, input_json TEXT, output_json TEXT)",
        "CREATE TABLE spans (id INTEGER GENERATED ALWAYS AS IDENTITY)",
        "INSERT INTO metrics_events VALUES ('rule_eval', 'allow'), ('rule_eval', 'block')",
        "INSERT INTO sessions VALUES "
        "('11111111-1111-1111-1111-111111111111', 'expired', NOW() - INTERVAL '48h'), "
        "('22222222-2222-2222-2222-222222222222', 'expired', NOW() - INTERVAL '1h'), "
        "('33333333-3333-3333-3333-333333333333', 'active', NOW() - INTERVAL '48h'), "
        "('00000000-0000-0000-0000-000000000001', 'expired', NOW() - INTERVAL '48h')",
        "INSERT INTO session_variables VALUES "
        "('11111111-1111-1111-1111-111111111111', 'expired'), "
        "('22222222-2222-2222-2222-222222222222', 'fresh'), "
        "('33333333-3333-3333-3333-333333333333', 'active'), "
        "('00000000-0000-0000-0000-000000000001', 'system')",
        "INSERT INTO token_events DEFAULT VALUES",
        "INSERT INTO loop_progress DEFAULT VALUES",
        "INSERT INTO step_executions VALUES "
        "('completed', '{\"input\": true}', '{\"output\": true}'), "
        "('running', '{\"input\": true}', NULL)",
        "INSERT INTO spans DEFAULT VALUES",
    )
    with psycopg.connect(database_url, autocommit=True) as connection:
        for statement in statements:
            connection.execute(statement)


def _purge_state(database_url: str) -> _PurgeState:
    with psycopg.connect(
        database_url,
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        return _PurgeState(
            metrics_allow=_count(
                connection,
                "SELECT count(*) AS count FROM metrics_events "
                "WHERE event_type = 'rule_eval' AND result = 'allow'",
            ),
            metrics_other=_count(
                connection,
                "SELECT count(*) AS count FROM metrics_events "
                "WHERE NOT (event_type = 'rule_eval' AND result = 'allow')",
            ),
            expired_variables=_count(
                connection,
                "SELECT count(*) AS count FROM session_variables sv JOIN sessions s "
                "ON s.id = sv.session_id WHERE s.status IN ('expired', 'deleted') "
                "AND s.id != '00000000-0000-0000-0000-000000000001' "
                "AND s.updated_at < CURRENT_TIMESTAMP - (24 * INTERVAL '1 hour')",
            ),
            variables_total=_count(
                connection,
                "SELECT count(*) AS count FROM session_variables",
            ),
            token_events=_count(connection, "SELECT count(*) AS count FROM token_events"),
            loop_progress=_count(
                connection,
                "SELECT count(*) AS count FROM loop_progress",
            ),
            completed_payloads=_count(
                connection,
                "SELECT count(*) AS count FROM step_executions WHERE status = 'completed' "
                "AND (input_json IS NOT NULL OR output_json IS NOT NULL)",
            ),
            running_payloads=_count(
                connection,
                "SELECT count(*) AS count FROM step_executions WHERE status = 'running' "
                "AND input_json IS NOT NULL",
            ),
            step_rows=_count(connection, "SELECT count(*) AS count FROM step_executions"),
            spans=_count(connection, "SELECT count(*) AS count FROM spans"),
        )


def _count(
    connection: psycopg.Connection[dict[str, object]],
    statement: str,
) -> int:
    row = connection.execute(statement).fetchone()
    assert row is not None
    return cast(int, row["count"])
