"""Lifecycle dispatch storage baseline tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import get_current_version, latest_known_version, run_migrations

pytestmark = pytest.mark.unit

TASK_COLUMNS = {
    "allow_automation",
    "unattended",
    "isolation",
    "assigned_agent",
    "additional_skills",
}
LEGACY_TASK_COLUMNS = {"lifecycle", "lifecycle_stage", "status"}
LIFECYCLE_TABLES = {
    "task_dispatch_mutex",
    "task_artifacts",
    "task_lifecycle_events",
}
LIFECYCLE_INDEXES = {
    "idx_tasks_dispatch_scan",
    "idx_dispatch_mutex_scan",
    "idx_lifecycle_events_task",
}


def _column_names(db: LocalDatabase, table: str) -> set[str]:
    return {row["name"] for row in db.fetchall(f"PRAGMA table_info({table})")}


def _table_names(db: LocalDatabase) -> set[str]:
    return {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}


def _index_names(db: LocalDatabase) -> set[str]:
    return {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type='index'")}


def _task_column(db: LocalDatabase, name: str) -> dict[str, Any]:
    row = db.fetchone("SELECT * FROM pragma_table_info('tasks') WHERE name = ?", (name,))
    assert row is not None
    return dict(row)


def _assert_lifecycle_schema(db: LocalDatabase) -> None:
    task_columns = _column_names(db, "tasks")
    assert TASK_COLUMNS.issubset(task_columns)
    assert LEGACY_TASK_COLUMNS.isdisjoint(task_columns)
    assert LIFECYCLE_TABLES.issubset(_table_names(db))
    assert LIFECYCLE_INDEXES.issubset(_index_names(db))
    assert _task_column(db, "allow_automation")["dflt_value"] == "0"
    assert _task_column(db, "unattended")["dflt_value"] == "0"
    assert _task_column(db, "isolation")["dflt_value"] == "'worktree'"

    artifacts_sql = db.fetchone(
        """
        SELECT sql
          FROM sqlite_master
         WHERE type = 'table'
           AND name = 'task_artifacts'
        """
    )["sql"]
    assert "(worktree_path IS NULL) = (worktree_id IS NULL)" in artifacts_sql
    assert "(clone_path IS NULL) = (clone_id IS NULL)" in artifacts_sql
    assert "(worktree_path IS NULL OR clone_path IS NULL)" in artifacts_sql


def test_fresh_database_gets_lifecycle_dispatch_schema(tmp_path: Path) -> None:
    db = LocalDatabase(tmp_path / "fresh-lifecycle.db")

    applied = run_migrations(db)

    assert applied == 2
    assert get_current_version(db) == latest_known_version()
    _assert_lifecycle_schema(db)


def test_existing_baseline_database_keeps_lifecycle_dispatch_schema(tmp_path: Path) -> None:
    db = LocalDatabase(tmp_path / "current-lifecycle.db")
    run_migrations(db)

    applied = run_migrations(db)

    assert applied == 0
    assert get_current_version(db) == latest_known_version()
    _assert_lifecycle_schema(db)
