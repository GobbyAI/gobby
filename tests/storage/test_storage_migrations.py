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


def test_tasks_table_includes_claimed_by_session_id_on_fresh_db(tmp_path) -> None:
    """Fresh baseline schema should include canonical task ownership."""
    db_path = tmp_path / "tasks_claim_owner.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    task_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(tasks)")}
    assert "claimed_by_session_id" in task_columns

    task_indexes = {row["name"] for row in db.fetchall("PRAGMA index_list(tasks)")}
    assert "idx_tasks_claimed_session" in task_indexes


def test_migration_208_recovers_when_column_exists_but_version_does_not(tmp_path) -> None:
    """Migration 208 should heal partial application without duplicate-column failure."""
    db_path = tmp_path / "tasks_claim_owner_partial.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    project_id = "00000000-0000-0000-0000-000000060887"
    session_id = "session-208"
    task_id = "task-208"
    db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (session_id, "ext-208", "machine-208", "codex", project_id),
    )
    db.execute(
        """
        INSERT INTO tasks (
            id, project_id, title, assignee, created_at, updated_at, claimed_by_session_id
        ) VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), NULL)
        """,
        (task_id, project_id, "partial migration task", session_id),
    )

    db.execute("DROP INDEX IF EXISTS idx_tasks_claimed_session")
    db.execute("DELETE FROM schema_version")
    db.execute("INSERT INTO schema_version (version) VALUES (207)")

    applied = run_migrations(db)

    assert applied == 1
    assert get_current_version(db) == EXPECTED_FINAL_VERSION
    task_row = db.fetchone(
        "SELECT claimed_by_session_id FROM tasks WHERE id = ?",
        (task_id,),
    )
    assert task_row is not None
    assert task_row["claimed_by_session_id"] == session_id

    task_indexes = {row["name"] for row in db.fetchall("PRAGMA index_list(tasks)")}
    assert "idx_tasks_claimed_session" in task_indexes


def test_migration_208_backfills_despite_legacy_orphaned_task_foreign_keys(tmp_path) -> None:
    """Migration 208 should recover even when legacy task rows violate older FKs."""
    db_path = tmp_path / "tasks_claim_owner_orphaned.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    project_id = "00000000-0000-0000-0000-000000060887"
    session_id = "session-208-valid"
    task_id = "task-208-orphaned"
    db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (session_id, "ext-208-valid", "machine-208", "codex", project_id),
    )

    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute(
            """
            INSERT INTO tasks (
                id,
                project_id,
                title,
                assignee,
                created_in_session_id,
                created_at,
                updated_at,
                claimed_by_session_id
            ) VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), NULL)
            """,
            (task_id, project_id, "orphaned task", session_id, "missing-session"),
        )
    finally:
        db.execute("PRAGMA foreign_keys=ON")

    db.execute("DROP INDEX IF EXISTS idx_tasks_claimed_session")
    db.execute("DELETE FROM schema_version")
    db.execute("INSERT INTO schema_version (version) VALUES (207)")

    applied = run_migrations(db)

    assert applied == 1
    task_row = db.fetchone(
        "SELECT claimed_by_session_id FROM tasks WHERE id = ?",
        (task_id,),
    )
    assert task_row is not None
    assert task_row["claimed_by_session_id"] == session_id


def test_get_current_version_error(tmp_path) -> None:
    """Test get_current_version handles errors (e.g. missing table)."""
    db_path = tmp_path / "error.db"
    db = LocalDatabase(db_path)

    # schema_version doesn't exist yet
    assert get_current_version(db) == 0

    # Mock execute to raise exception even if table exists logic was reached
    with patch.object(db, "fetchone", side_effect=sqlite3.OperationalError("Boom")):
        assert get_current_version(db) == 0
