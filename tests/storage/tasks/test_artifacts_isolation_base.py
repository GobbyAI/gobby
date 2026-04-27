"""Tests for task_artifacts base metadata migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import MIGRATIONS, _run_migration_list, run_migrations

pytestmark = pytest.mark.unit


def test_migration_adds_column_nullable(tmp_path: Path) -> None:
    db = LocalDatabase(tmp_path / "fresh.db")
    run_migrations(db)

    columns = _column_info(db, "task_artifacts")

    assert columns["base_commit_sha"]["notnull"] == 0
    assert columns["plan_file_hash"]["notnull"] == 0


def test_migration_preserves_legacy_rows(tmp_path: Path) -> None:
    db = _legacy_artifacts_db(tmp_path)

    version, _description, _action = _evidence_migration()
    _run_migration_list(db, version - 1, [_evidence_migration()])

    row = db.fetchone("SELECT * FROM task_artifacts WHERE task_id = ?", ("task-1",))
    assert row is not None
    assert row["plan_file_path"] == ".gobby/plans/legacy.md"
    assert row["worktree_path"] == "/tmp/wt"
    assert row["worktree_id"] == "wt-1"
    assert row["target_branch"] == "main"
    assert row["base_commit_sha"] is None
    assert row["plan_file_hash"] is None


def test_baseline_schema_matches_post_migration(tmp_path: Path) -> None:
    migrated = _legacy_artifacts_db(tmp_path / "legacy")
    version, _description, _action = _evidence_migration()
    _run_migration_list(migrated, version - 1, [_evidence_migration()])
    fresh = LocalDatabase(tmp_path / "fresh.db")
    run_migrations(fresh)

    assert _column_signature(migrated, "task_artifacts") == _column_signature(
        fresh,
        "task_artifacts",
    )
    fresh_sql = _table_sql(fresh, "task_artifacts")
    migrated_sql = _table_sql(migrated, "task_artifacts")
    assert "base_commit_sha IS NULL" in fresh_sql
    assert "base_commit_sha IS NULL" in migrated_sql


def _legacy_artifacts_db(tmp_path: Path) -> LocalDatabase:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = LocalDatabase(tmp_path / "legacy.db")
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO schema_version (version) VALUES (222)")
    db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO tasks (id) VALUES ('task-1')")
    db.execute(
        """
        CREATE TABLE task_artifacts (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            plan_file_path TEXT,
            worktree_path TEXT,
            worktree_id TEXT,
            clone_path TEXT,
            clone_id TEXT,
            target_branch TEXT,
            expansion_run_id TEXT,
            expansion_attempts INTEGER NOT NULL DEFAULT 0,
            pr_url TEXT,
            merge_commit_sha TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                (worktree_path IS NULL) = (worktree_id IS NULL)
                AND (clone_path IS NULL) = (clone_id IS NULL)
                AND (worktree_path IS NULL OR clone_path IS NULL)
            )
        )
        """
    )
    db.execute(
        """
        INSERT INTO task_artifacts (
            task_id, plan_file_path, worktree_path, worktree_id, target_branch
        )
        VALUES ('task-1', '.gobby/plans/legacy.md', '/tmp/wt', 'wt-1', 'main')
        """
    )
    return db


def _evidence_migration() -> tuple[int, str, Any]:
    """Locate the migration that adds evidence-metadata columns to task_artifacts.

    The version number has shifted as new migrations have been registered, so
    look the migration up by its stable description rather than a hard-coded
    version.
    """
    return next(
        migration
        for migration in MIGRATIONS
        if migration[1] == "Add evidence metadata to task_artifacts"
    )


def _column_info(db: LocalDatabase, table: str) -> dict[str, dict[str, Any]]:
    return {row["name"]: dict(row) for row in db.fetchall(f"PRAGMA table_info({table})")}


def _column_signature(db: LocalDatabase, table: str) -> list[tuple[Any, ...]]:
    return [
        (row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
        for row in db.fetchall(f"PRAGMA table_info({table})")
    ]


def _table_sql(db: LocalDatabase, table: str) -> str:
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
    assert row is not None
    return str(row["sql"])
