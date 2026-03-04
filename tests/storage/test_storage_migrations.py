import sqlite3
from unittest.mock import patch

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MIGRATIONS,
    get_current_version,
    run_migrations,
)

pytestmark = pytest.mark.unit

# Calculate expected version after all migrations
EXPECTED_FINAL_VERSION = max(
    BASELINE_VERSION,
    max((m[0] for m in MIGRATIONS), default=BASELINE_VERSION),
)


def test_migrations_fresh_db(tmp_path) -> None:
    """Test running migrations on a fresh database.

    With the baseline schema architecture:
    - Fresh databases get BASELINE_SCHEMA applied directly (counts as 1 migration)
    - Plus any incremental migrations beyond the baseline
    - Final version is EXPECTED_FINAL_VERSION
    """
    db_path = tmp_path / "migration_test.db"
    db = LocalDatabase(db_path)

    # Initial state
    assert get_current_version(db) == 0

    # Run migrations
    applied = run_migrations(db)

    # Fresh databases apply baseline schema + incremental migrations
    expected_count = 1 + len([m for m in MIGRATIONS if m[0] > BASELINE_VERSION])
    assert applied == expected_count

    # Verify version reaches expected final version
    current_version = get_current_version(db)
    assert current_version == EXPECTED_FINAL_VERSION

    # Check tables exist (sample check)
    tables = [
        "schema_version",
        "projects",
        "sessions",
        "mcp_servers",
        "tools",
        "tasks",
        "task_dependencies",
        "session_tasks",
        "session_messages",
        "memories",
        "tool_embeddings",
        "task_validation_history",
        "workflow_definitions",
    ]
    for table in tables:
        # Check if table exists in sqlite_master
        row = db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        assert row is not None, f"Table {table} not created"


def test_migrations_idempotency(tmp_path) -> None:
    """Test that running migrations again does nothing."""
    db_path = tmp_path / "idempotency.db"
    db = LocalDatabase(db_path)

    run_migrations(db)
    initial_version = get_current_version(db)
    assert initial_version == EXPECTED_FINAL_VERSION

    # Run again
    applied = run_migrations(db)
    assert applied == 0
    assert get_current_version(db) == initial_version


def test_get_current_version_error(tmp_path) -> None:
    """Test get_current_version handles errors (e.g. missing table)."""
    db_path = tmp_path / "error.db"
    db = LocalDatabase(db_path)

    # schema_version doesn't exist yet
    assert get_current_version(db) == 0

    # Mock execute to raise exception even if table exists logic was reached
    with patch.object(db, "fetchone", side_effect=sqlite3.OperationalError("Boom")):
        assert get_current_version(db) == 0
