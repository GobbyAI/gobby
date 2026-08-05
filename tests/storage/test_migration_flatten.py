"""Integration contracts for the one-time migration-bookkeeping flatten cutover."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from gobby.storage.migration_flatten import (
    FlattenEvidence,
    MigrationReceipt,
    cutover_migration_bookkeeping,
    load_flatten_evidence,
)

pytestmark = pytest.mark.integration


def test_packaged_flatten_evidence_matches_generated_baseline() -> None:
    evidence = load_flatten_evidence()

    assert evidence.baseline_version == 375
    assert evidence.baseline_checksum == (
        "eaf97c2662053cf0f3b112410d66b7bc123402f1100224873656bbe199bc7a80"
    )
    assert evidence.applied_versions[-1] == 375
    assert evidence.receipts[-1].filename == "375_machine_scope.sql"


@pytest.fixture
def flatten_database(postgres_database_url: str) -> Iterator[tuple[str, uuid.UUID]]:
    database_name = f"gobby_test_flatten_{uuid.uuid4().hex}"
    database_url = make_conninfo(postgres_database_url, dbname=database_name)
    epoch_id = uuid.uuid4()
    with psycopg.connect(postgres_database_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    filename TEXT,
                    checksum TEXT,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE maintenance_epochs (
                    id UUID PRIMARY KEY,
                    campaign TEXT NOT NULL,
                    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    opened_by TEXT NOT NULL,
                    scope_note TEXT NOT NULL,
                    released_at TIMESTAMPTZ,
                    released_by_command TEXT
                );
                """
            )
            connection.execute(
                """
                INSERT INTO maintenance_epochs(id, campaign, opened_by, scope_note)
                VALUES (%s, 'flatten', 'hub-maintenance:flatten', 'test cutover')
                """,
                (epoch_id,),
            )
        yield database_url, epoch_id
    finally:
        with psycopg.connect(postgres_database_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )


def _evidence() -> FlattenEvidence:
    return FlattenEvidence(
        baseline_version=375,
        baseline_checksum="f" * 64,
        applied_versions=(353, 354, 375),
        receipts=(
            MigrationReceipt(354, "354_bookkeeping.sql", "a" * 64),
            MigrationReceipt(375, "375_machine_scope.sql", "b" * 64),
        ),
    )


def _seed_receipts(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(
            """
            INSERT INTO schema_migrations(version, filename, checksum)
            VALUES
                (353, NULL, NULL),
                (354, '354_bookkeeping.sql', %s),
                (375, '375_machine_scope.sql', %s)
            """,
            ("a" * 64, "b" * 64),
        )


def test_cutover_holds_migration_lock_and_replaces_rows_atomically(
    flatten_database: tuple[str, uuid.UUID],
) -> None:
    database_url, epoch_id = flatten_database
    _seed_receipts(database_url)
    lock_observations: list[bool] = []

    def observe_lock(point: str) -> None:
        if point != "after_delete":
            return
        with psycopg.connect(database_url, autocommit=True) as contender:
            acquired = contender.execute(
                """
                SELECT pg_try_advisory_lock(
                    hashtext('postgres_migrations_apply'),
                    hashtext(current_schema())
                )
                """
            ).fetchone()
        lock_observations.append(bool(acquired and acquired[0]))

    cutover_migration_bookkeeping(
        database_url,
        epoch_id,
        _evidence(),
        fault_hook=observe_lock,
    )

    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            "SELECT version, filename, checksum FROM schema_migrations"
        ).fetchall()
    assert lock_observations == [False]
    assert rows == [(375, "baseline@375", "f" * 64)]


def test_cutover_crash_after_delete_rolls_back_all_receipts(
    flatten_database: tuple[str, uuid.UUID],
) -> None:
    database_url, epoch_id = flatten_database
    _seed_receipts(database_url)

    def crash(point: str) -> None:
        if point == "after_delete":
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        cutover_migration_bookkeeping(
            database_url,
            epoch_id,
            _evidence(),
            fault_hook=crash,
        )

    with psycopg.connect(database_url) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert versions == [(353,), (354,), (375,)]


def test_cutover_rejects_receipt_skew_before_bookkeeping_change(
    flatten_database: tuple[str, uuid.UUID],
) -> None:
    database_url, epoch_id = flatten_database
    _seed_receipts(database_url)
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 354")

    with pytest.raises(RuntimeError, match="receipt mismatch.*v354"):
        cutover_migration_bookkeeping(database_url, epoch_id, _evidence())

    with psycopg.connect(database_url) as connection:
        count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
    assert count == (3,)
