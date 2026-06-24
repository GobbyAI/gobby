from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.integration
def test_select_star_after_shape_change_avoids_cached_plan_result_error(
    postgres_db: Any,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    table = "prepared_statement_shape_change"
    _drop_table(postgres_db, table)
    postgres_db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, name TEXT NOT NULL)')
    postgres_db.execute(f'INSERT INTO "{table}" (id, name) VALUES (%s, %s)', (1, "before"))

    try:
        with postgres_db.transaction() as txn:
            for _ in range(6):
                row = txn.execute(f'SELECT * FROM "{table}" WHERE id = %s', (1,)).fetchone()
                assert row is not None
                assert row["name"] == "before"

            txn.execute(f"ALTER TABLE \"{table}\" ADD COLUMN note TEXT DEFAULT 'after'")
            try:
                row = txn.execute(f'SELECT * FROM "{table}" WHERE id = %s', (1,)).fetchone()
            except psycopg.errors.FeatureNotSupported as exc:
                pytest.fail(f"cached prepared plan changed result type: {exc}")

            assert row is not None
            assert row["note"] == "after"
    finally:
        _drop_table(postgres_db, table)


def _drop_table(postgres_db: Any, table: str) -> None:
    postgres_db.execute(f'DROP TABLE IF EXISTS "{table}"')
