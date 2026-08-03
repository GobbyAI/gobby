"""Focused integration tests for the staged machine-identity cutover."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from gobby.storage.identity_cutover import (
    IdentityCutoverError,
    run_identity_cutover,
    verify_identity_cutover,
)
from gobby.storage.migrations import _execute_sql_script

pytestmark = pytest.mark.integration


@pytest.fixture
def legacy_identity_db(postgres_database_url: str) -> Iterator[str]:
    database_name = f"gobby_test_identity_{uuid.uuid4().hex}"
    scoped_dsn = make_conninfo(postgres_database_url, dbname=database_name)
    with psycopg.connect(postgres_database_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    try:
        with psycopg.connect(scoped_dsn, autocommit=True) as connection:
            connection.execute(
                """
                CREATE TABLE machines (
                    machine_id TEXT PRIMARY KEY,
                    hostname TEXT,
                    os TEXT,
                    label TEXT,
                    tailscale_name TEXT,
                    owner_user_id TEXT,
                    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE sessions (
                    id UUID PRIMARY KEY,
                    machine_id TEXT NOT NULL
                );
                CREATE TABLE bin_update_state (
                    tool_name TEXT PRIMARY KEY,
                    installed_version TEXT,
                    floor_version TEXT NOT NULL,
                    latest_version TEXT,
                    binary_path TEXT,
                    target TEXT,
                    last_status TEXT NOT NULL,
                    last_error TEXT,
                    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    installed_at TIMESTAMPTZ,
                    source_url TEXT,
                    is_dev BOOLEAN NOT NULL DEFAULT FALSE,
                    floor_drift BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            precursor = Path(
                "src/gobby/storage/migrations/364_identity_cutover_journal.sql"
            ).read_text()
            _execute_sql_script(connection, precursor)
        yield scoped_dsn
    finally:
        with psycopg.connect(postgres_database_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )


def _seed_legacy_inventory(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            """
            INSERT INTO machines(machine_id, hostname) VALUES
                ('local-legacy', 'local-host'),
                ('retired-legacy', 'retired-host')
            """
        )
        connection.execute(
            """
            INSERT INTO sessions(id, machine_id) VALUES
                (%s, 'local-legacy'),
                (%s, 'retired-legacy'),
                (%s, 'orphan-legacy')
            """,
            (uuid.uuid4(), uuid.uuid4(), uuid.uuid4()),
        )
        connection.execute(
            """
            INSERT INTO bin_update_state(tool_name, floor_version, last_status)
            VALUES ('ghook', '0.4.0', 'up_to_date')
            """
        )


def test_cutover_retires_inventory_rewrites_file_and_converts_schema(
    legacy_identity_db: str, tmp_path: Path
) -> None:
    _seed_legacy_inventory(legacy_identity_db)
    identity_file = tmp_path / "machine_id"
    identity_file.write_text("local-legacy")

    report = run_identity_cutover(legacy_identity_db, identity_file)

    new_id = str(uuid.UUID(identity_file.read_text()))
    assert report.rotated_id == new_id
    with psycopg.connect(legacy_identity_db, row_factory=dict_row) as connection:
        machines = connection.execute("SELECT * FROM machines").fetchall()
        sessions = connection.execute(
            "SELECT machine_id FROM sessions ORDER BY machine_id NULLS LAST"
        ).fetchall()
        tombstones = connection.execute(
            "SELECT old_id FROM retired_machine_identities ORDER BY old_id"
        ).fetchall()
        phases = connection.execute(
            "SELECT DISTINCT phase FROM identity_cutover_journal"
        ).fetchall()

    assert [row["machine_id"] for row in machines] == [new_id]
    assert [row["machine_id"] for row in sessions] == [new_id, None, None]
    assert [row["old_id"] for row in tombstones] == ["orphan-legacy", "retired-legacy"]
    assert phases == [{"phase": "file_committed"}]
    verify_identity_cutover(legacy_identity_db, identity_file)

    migration = Path("src/gobby/storage/migrations/365_machines_uuid_identity.sql").read_text()
    with psycopg.connect(legacy_identity_db, autocommit=True, row_factory=dict_row) as connection:
        _execute_sql_script(connection, migration)
        machine_columns = connection.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'machines'
            ORDER BY ordinal_position
            """
        ).fetchall()
        bin_row = connection.execute(
            "SELECT machine_id::text AS machine_id, tool_name FROM bin_update_state"
        ).fetchone()
        session_column = connection.execute(
            """
            SELECT data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'sessions'
              AND column_name = 'machine_id'
            """
        ).fetchone()

    assert {row["column_name"] for row in machine_columns} >= {"id", "hostname", "last_seen"}
    assert "machine_id" not in {row["column_name"] for row in machine_columns}
    assert next(row for row in machine_columns if row["column_name"] == "id")["data_type"] == "uuid"
    assert bin_row == {"machine_id": new_id, "tool_name": "ghook"}
    # This is migration 366's required input shape: 364 made it nullable,
    # cutover wrote NULLs, and 365 deliberately leaves the TEXT column intact.
    assert session_column == {"data_type": "text", "is_nullable": "YES"}


@pytest.mark.parametrize(
    "fault_point",
    ["after_db_commit", "after_file_replace", "after_file_commit"],
)
def test_cutover_resumes_per_identity_and_phase(
    legacy_identity_db: str, tmp_path: Path, fault_point: str
) -> None:
    _seed_legacy_inventory(legacy_identity_db)
    identity_file = tmp_path / "machine_id"
    identity_file.write_text("local-legacy")

    def crash(point: str, old_id: str) -> None:
        if point == fault_point and old_id == "local-legacy":
            raise RuntimeError(f"crash at {point}")

    with pytest.raises(RuntimeError, match="crash at"):
        run_identity_cutover(legacy_identity_db, identity_file, fault_injector=crash)

    with psycopg.connect(legacy_identity_db) as connection:
        phases: dict[str, str] = dict(
            connection.execute(
                "SELECT old_id, phase FROM identity_cutover_journal"
            ).fetchall()
        )
        new_id_row = connection.execute(
            "SELECT new_id FROM identity_cutover_journal WHERE old_id = 'local-legacy'"
        ).fetchone()
        assert new_id_row is not None
        new_id = str(new_id_row[0])
    assert phases["orphan-legacy"] == "file_committed"
    assert phases["retired-legacy"] == "file_committed"
    assert phases["local-legacy"] == (
        "file_committed" if fault_point == "after_file_commit" else "db_committed"
    )
    assert identity_file.read_text() == (
        "local-legacy" if fault_point == "after_db_commit" else new_id
    )

    report = run_identity_cutover(legacy_identity_db, identity_file)

    assert report.completed_identities == 3
    verify_identity_cutover(legacy_identity_db, identity_file)


def test_cutover_preflight_rejects_foreign_application_connection(
    legacy_identity_db: str, tmp_path: Path
) -> None:
    _seed_legacy_inventory(legacy_identity_db)
    identity_file = tmp_path / "machine_id"
    identity_file.write_text("local-legacy")

    with psycopg.connect(legacy_identity_db, application_name="gobby-old-daemon"):
        with pytest.raises(IdentityCutoverError, match="foreign application connection"):
            run_identity_cutover(legacy_identity_db, identity_file)

    run_identity_cutover(legacy_identity_db, identity_file)
    verify_identity_cutover(legacy_identity_db, identity_file)


def test_activation_fence_rejects_legacy_and_uuid_shaped_writers(
    legacy_identity_db: str, tmp_path: Path
) -> None:
    _seed_legacy_inventory(legacy_identity_db)
    identity_file = tmp_path / "machine_id"
    identity_file.write_text("local-legacy")

    def stop_after_activation(point: str, _old_id: str) -> None:
        if point == "after_activation":
            raise RuntimeError("activated")

    with pytest.raises(RuntimeError, match="activated"):
        run_identity_cutover(
            legacy_identity_db,
            identity_file,
            fault_injector=stop_after_activation,
        )

    with psycopg.connect(legacy_identity_db, autocommit=True) as old_writer:
        for candidate in ("late-legacy", str(uuid.uuid4())):
            with pytest.raises(psycopg.DatabaseError, match="identity cutover fence"):
                old_writer.execute(
                    "INSERT INTO machines(machine_id) VALUES (%s)",
                    (candidate,),
                )


def test_destructive_guard_refuses_half_remapped_machine(
    legacy_identity_db: str,
) -> None:
    with psycopg.connect(legacy_identity_db, autocommit=True) as connection:
        connection.execute("INSERT INTO machines(machine_id) VALUES ('legacy')")
        connection.execute(
            """
            INSERT INTO identity_cutover_journal(
                old_id, new_id, disposition, phase, token, had_machine, session_count
            ) VALUES ('legacy', %s, 'rotated', 'started', %s, TRUE, 0)
            """,
            (uuid.uuid4(), uuid.uuid4()),
        )
        migration = Path("src/gobby/storage/migrations/365_machines_uuid_identity.sql").read_text()

        with pytest.raises(psycopg.DatabaseError, match="not file_committed"):
            _execute_sql_script(connection, migration)
