"""Tests for pending_interactions table migration.

Verifies that the pending_interactions table is created with the correct schema,
columns, indexes, and constraints after running migrations on a fresh DB.
"""

from __future__ import annotations

import sqlite3

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def fresh_db(tmp_path) -> LocalDatabase:  # type: ignore[type-arg]
    """Create a fresh database with all migrations applied."""
    db_path = tmp_path / "pending_interactions_test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)
    return db


def test_pending_interactions_table_exists(fresh_db: LocalDatabase) -> None:
    """Fresh DB has the pending_interactions table."""
    row = fresh_db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_interactions'"
    )
    assert row is not None, "pending_interactions table should exist after migrations"


def test_pending_interactions_columns(fresh_db: LocalDatabase) -> None:
    """Table has all required columns with correct names."""
    rows = fresh_db.fetchall("PRAGMA table_info(pending_interactions)")
    column_names = {row["name"] for row in rows}

    expected_columns = {
        "id",
        "session_id",
        "kind",
        "provider",
        "tool_name",
        "payload_json",
        "status",
        "decision",
        "response_json",
        "timeout_seconds",
        "created_at",
        "resolved_at",
    }

    assert expected_columns.issubset(column_names), (
        f"Missing columns: {expected_columns - column_names}"
    )


def test_pending_interactions_session_status_index(fresh_db: LocalDatabase) -> None:
    """Index exists on (session_id, status)."""
    rows = fresh_db.fetchall("PRAGMA index_list(pending_interactions)")
    index_names = [row["name"] for row in rows]

    # Look for the composite index by inspecting each index's columns
    found = False
    for idx_name in index_names:
        cols_rows = fresh_db.fetchall(f"PRAGMA index_info('{idx_name}')")
        col_names = [r["name"] for r in cols_rows]
        if col_names == ["session_id", "status"]:
            found = True
            break

    assert found, (
        "Expected an index on (session_id, status) in pending_interactions. "
        f"Found indexes: {index_names}"
    )


def test_pending_interactions_unique_partial_index(fresh_db: LocalDatabase) -> None:
    """Unique partial index on (session_id, kind) WHERE status = 'pending'."""
    rows = fresh_db.fetchall("PRAGMA index_list(pending_interactions)")

    # Find unique indexes that cover (session_id, kind)
    found_unique = False
    for row in rows:
        if not row["unique"]:
            continue
        idx_name = row["name"]
        cols_rows = fresh_db.fetchall(f"PRAGMA index_info('{idx_name}')")
        col_names = [r["name"] for r in cols_rows]
        if col_names == ["session_id", "kind"]:
            found_unique = True
            break

    assert found_unique, (
        "Expected a unique partial index on (session_id, kind) WHERE status='pending'"
    )

    # Verify the partial index by testing the constraint: inserting two rows with
    # same (session_id, kind) and status='pending' should fail
    with fresh_db.transaction() as conn:
        # Create stub project and session to satisfy FK constraints
        conn.execute(
            "INSERT INTO projects (id, name, created_at) VALUES ('proj-1', 'test', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
            "VALUES ('sess-1', 'ext-1', 'machine-1', 'claude', 'proj-1', datetime('now'), datetime('now'))"
        )
        # First insert should succeed
        conn.execute(
            """
            INSERT INTO pending_interactions
                (id, session_id, kind, provider, tool_name, payload_json, status, timeout_seconds, created_at)
            VALUES
                ('int-1', 'sess-1', 'tool_approval', 'claude', 'bash', '{}', 'pending', 30, datetime('now'))
            """
        )
        # Second with same (session_id, kind) and status='pending' should fail
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO pending_interactions
                    (id, session_id, kind, provider, tool_name, payload_json, status, timeout_seconds, created_at)
                VALUES
                    ('int-2', 'sess-1', 'tool_approval', 'claude', 'bash', '{}', 'pending', 30, datetime('now'))
                """
            )


def test_pending_interactions_allows_resolved_duplicates(fresh_db: LocalDatabase) -> None:
    """Multiple resolved rows with same (session_id, kind) should be allowed."""
    with fresh_db.transaction() as conn:
        # Create stub project and session to satisfy FK constraints
        conn.execute(
            "INSERT INTO projects (id, name, created_at) VALUES ('proj-1', 'test', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
            "VALUES ('sess-1', 'ext-1', 'machine-1', 'claude', 'proj-1', datetime('now'), datetime('now'))"
        )
        for i in range(3):
            conn.execute(
                """
                INSERT INTO pending_interactions
                    (id, session_id, kind, provider, tool_name, payload_json, status, timeout_seconds, created_at)
                VALUES
                    (?, 'sess-1', 'tool_approval', 'claude', 'bash', '{}', 'resolved', 30, datetime('now'))
                """,
                (f"int-{i}",),
            )

    rows = fresh_db.fetchall(
        "SELECT id FROM pending_interactions WHERE session_id='sess-1' AND kind='tool_approval'"
    )
    assert len(rows) == 3
