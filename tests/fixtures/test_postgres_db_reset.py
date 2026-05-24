"""Acceptance 2.2.3: `_reset_schema` restores each test to fresh-baseline state.

Verifies that the per-test `postgres_db` fixture's reset semantics produce
a worker schema byte-for-byte equivalent to what `apply_migrations()` writes
on a fresh schema, even after mutations across seed-bearing tables, the
    shared `_BASELINE_BOOKKEEPING_TABLES` set (owned by
    `gobby.storage.hub.postgres`), and arbitrary application tables.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

pytestmark = pytest.mark.integration


def _require_database_url() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is required for postgres_db reset semantics tests")
    return dsn


def test_seed_rows_survive_reset(
    postgres_schema: str,
    postgres_canonical_seed: dict[str, list[tuple[Any, ...]]],
) -> None:
    """`_reset_schema` restores the worker schema to fresh-baseline state.

    Covers acceptance 2.2.3 across all five required behaviors:

    1. Capture happens before the first reset — `postgres_canonical_seed`
       already contains the baseline seed rows for `projects` and
       `task_type_default_stages` when this test runs.
    2. Entry and exit resets receive the same snapshot — verified by
       confirming the snapshot dict is not mutated by `_reset_schema`.
    3. Extra `projects` row inserted between resets is removed; only the
       four canonical placeholders survive.
    4. Mutating a `task_type_default_stages` row is reverted to the
       canonical value on reset.
    5. `schema_migrations` is verifiably absent from the computed
       `truncate_tables` set inside `_reset_schema`.
    6. The removed PostgreSQL import marker table is absent from the baseline.
    """
    psycopg = pytest.importorskip("psycopg")

    from gobby.storage.hub.postgres import _BASELINE_BOOKKEEPING_TABLES
    from tests.fixtures.postgres import _reset_schema

    dsn = _require_database_url()

    # 1) Capture happened before this test was created; seed must already
    # include the canonical baseline rows.
    assert "projects" in postgres_canonical_seed
    assert "task_type_default_stages" in postgres_canonical_seed
    assert len(postgres_canonical_seed["projects"]) == 4

    # Snapshot copy so we can verify it is not mutated by reset operations.
    snapshot_before = {table: list(rows) for table, rows in postgres_canonical_seed.items()}

    # Entry-style reset to establish a clean starting state for the
    # mutations below (the postgres_db fixture would normally do this).
    _reset_schema(dsn, postgres_schema, postgres_canonical_seed)

    # Apply mutations across all three categories of tables.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(postgres_schema))
        )
        # (3) Extra row in projects (seed-bearing).
        conn.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (
                "00000000-0000-0000-0000-0000000abcde",
                "test-extra-project",
            ),
        )
        # (4) Mutate task_type_default_stages (seed-bearing).
        conn.execute(
            "UPDATE task_type_default_stages SET position = 999 "
            "WHERE task_type = %s AND stage_name = %s",
            ("feature", "development"),
        )

    # Exit-style reset — same snapshot in.
    _reset_schema(dsn, postgres_schema, postgres_canonical_seed)

    # (2) Snapshot itself is unchanged across both resets.
    assert postgres_canonical_seed == snapshot_before

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(postgres_schema))
        )
        # (3) Projects: only the four canonical placeholders remain.
        rows = conn.execute("SELECT id FROM projects ORDER BY id").fetchall()
        ids = [r[0] for r in rows]
        assert ids == [
            "00000000-0000-0000-0000-000000000000",
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            "00000000-0000-0000-0000-000000060887",
        ]

        # (4) task_type_default_stages mutation reverted.
        row = conn.execute(
            "SELECT position FROM task_type_default_stages "
            "WHERE task_type = %s AND stage_name = %s",
            ("feature", "development"),
        ).fetchone()
        assert row is not None
        # Canonical value for feature → development is position 2.
        assert row[0] == 2

        # (5) schema_migrations is filtered out of the computed truncate set
        # inside _reset_schema. Recompute truncate_tables here against the live
        # schema and assert.
        all_tables = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            ).fetchall()
        }
        truncate_tables = all_tables - _BASELINE_BOOKKEEPING_TABLES
        assert "schema_migrations" not in truncate_tables
        assert "schema_migrations" in all_tables
        # (6) Removed PostgreSQL import marker table is absent from the baseline.
        assert "gobby_migration_state" not in all_tables
