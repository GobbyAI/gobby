"""Tests for task_artifacts baseline metadata."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def test_migration_adds_column_nullable(temp_db: HubDatabase) -> None:
    db = temp_db
    columns = _column_info(db, "task_artifacts")

    assert columns["base_commit_sha"]["is_nullable"] == "YES"
    assert columns["plan_file_hash"]["is_nullable"] == "YES"


def _column_info(db: HubDatabase, table: str) -> dict[str, dict[str, Any]]:
    return {
        row["column_name"]: dict(row)
        for row in db.fetchall(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_name = %s
            """,
            (table,),
        )
    }
