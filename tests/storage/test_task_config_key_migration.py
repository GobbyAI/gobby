"""Exercise migration 434 with connection-local tables on the isolated test hub."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "crates/gcore/assets/schema/migrations/434_canonical_task_config_keys.sql"
).read_text()


@pytest.fixture
def connection() -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.transaction(force_rollback=True):
            conn.execute("CREATE TEMP TABLE config_state (id boolean PRIMARY KEY, revision bigint)")
            conn.execute(
                "CREATE TEMP TABLE config_store (key text PRIMARY KEY, value text NOT NULL, "
                "source text NOT NULL DEFAULT 'user', is_secret boolean NOT NULL DEFAULT false, "
                "updated_at timestamptz NOT NULL DEFAULT now(), revision bigint NOT NULL DEFAULT 0)"
            )
            conn.execute("INSERT INTO config_state VALUES (true, 7)")
            yield conn


def test_migration_preserves_values_metadata_and_other_namespaces(
    connection: psycopg.Connection[tuple[Any, ...]],
) -> None:
    rows = [
        ("gobby_tasks.validation.max_iterations", "7", "user", False, 5),
        ("gobby_tasks.expansion.pattern_criteria.patterns.audit", '["Verified"]', "user", False, 6),
        ("gobby_tasks.validation.system_prompt", '"$secret:existing"', "import", True, 7),
        ("memory.enabled", "false", "user", False, 4),
    ]
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO config_store (key, value, source, is_secret, revision) "
            "VALUES (%s, %s, %s, %s, %s)",
            rows,
        )
    timestamps = connection.execute("SELECT DISTINCT updated_at FROM config_store").fetchall()
    connection.execute(MIGRATION)
    assert connection.execute("SELECT revision FROM config_state").fetchone() == (8,)
    actual = connection.execute(
        "SELECT key, value, source, is_secret, revision FROM config_store ORDER BY key"
    ).fetchall()
    expected = sorted(
        (
            key.replace("gobby_tasks.", "gobby-tasks.", 1),
            value,
            source,
            secret,
            8 if key.startswith("gobby_tasks.") else revision,
        )
        for key, value, source, secret, revision in rows
    )
    assert actual == expected
    assert (
        connection.execute("SELECT DISTINCT updated_at FROM config_store").fetchall() == timestamps
    )
    connection.execute(MIGRATION)
    assert connection.execute("SELECT revision FROM config_state").fetchone() == (8,)


def test_empty_migration_does_not_advance_revision(
    connection: psycopg.Connection[tuple[Any, ...]],
) -> None:
    connection.execute(MIGRATION)
    assert connection.execute("SELECT revision FROM config_state").fetchone() == (7,)


@pytest.mark.parametrize("collision", [False, True])
def test_migration_failure_preserves_original_rows(
    connection: psycopg.Connection[tuple[Any, ...]], collision: bool
) -> None:
    connection.execute(
        "INSERT INTO config_store (key, value) VALUES ('gobby_tasks.enabled', 'false')"
    )
    if collision:
        connection.execute(
            "INSERT INTO config_store (key, value) VALUES ('gobby-tasks.enabled', 'true')"
        )
    else:
        connection.execute("UPDATE config_state SET revision = 9007199254740991")
    before = connection.execute("SELECT * FROM config_store ORDER BY key").fetchall()
    revision = connection.execute("SELECT revision FROM config_state").fetchone()
    with pytest.raises(psycopg.errors.RaiseException, match="collision|exhausted"):
        with connection.transaction():
            connection.execute(MIGRATION)
    assert connection.execute("SELECT * FROM config_store ORDER BY key").fetchall() == before
    assert connection.execute("SELECT revision FROM config_state").fetchone() == revision
