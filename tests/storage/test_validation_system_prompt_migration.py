"""Migration 445 drops the stored override for a key the registry no longer has."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.unit

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "crates/gcore/assets/schema/migrations/445_drop_task_validation_system_prompt.sql"
).read_text()

REMOVED_KEY = "gobby-tasks.validation.system_prompt"


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


def test_migration_drops_residual_key_and_advances_revision(
    connection: psycopg.Connection[tuple[Any, ...]],
) -> None:
    rows = [
        (REMOVED_KEY, '"Validate."', "user", False, 5),
        ("gobby-tasks.validation.criteria_system_prompt", '"Judge."', "user", False, 6),
        ("memory.enabled", "false", "user", False, 4),
    ]
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO config_store (key, value, source, is_secret, revision) "
            "VALUES (%s, %s, %s, %s, %s)",
            rows,
        )
    connection.execute(MIGRATION)
    assert connection.execute("SELECT revision FROM config_state").fetchone() == (8,)
    actual = connection.execute(
        "SELECT key, value, source, is_secret, revision FROM config_store ORDER BY key"
    ).fetchall()
    assert actual == sorted(row for row in rows if row[0] != REMOVED_KEY)
    connection.execute(MIGRATION)
    assert connection.execute("SELECT revision FROM config_state").fetchone() == (8,)


def test_empty_migration_does_not_advance_revision(
    connection: psycopg.Connection[tuple[Any, ...]],
) -> None:
    connection.execute(MIGRATION)
    assert connection.execute("SELECT revision FROM config_state").fetchone() == (7,)
