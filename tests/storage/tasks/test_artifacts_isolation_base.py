"""Tests for task_artifacts baseline metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.storage.database import LocalDatabase
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit


def test_migration_adds_column_nullable(tmp_path: Path) -> None:
    db = LocalDatabase(tmp_path / "fresh.db")
    run_migrations(db)

    columns = _column_info(db, "task_artifacts")

    assert columns["base_commit_sha"]["notnull"] == 0
    assert columns["plan_file_hash"]["notnull"] == 0


def _column_info(db: LocalDatabase, table: str) -> dict[str, dict[str, Any]]:
    return {row["name"]: dict(row) for row in db.fetchall(f"PRAGMA table_info({table})")}


def _table_sql(db: LocalDatabase, table: str) -> str:
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
    assert row is not None
    return str(row["sql"])
