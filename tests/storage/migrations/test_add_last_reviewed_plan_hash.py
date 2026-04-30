"""Migration coverage for lifecycle review artifact fields."""

from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.unit


def test_round_trip(temp_db) -> None:
    migration = importlib.import_module("gobby.storage.migrations.add_last_reviewed_plan_hash")

    migration.up(temp_db)
    columns = {row["name"] for row in temp_db.fetchall("PRAGMA table_info(task_artifacts)")}

    assert "last_reviewed_plan_hash" in columns
    assert "plan_review_attempts" in columns
    assert "merge_attempts" in columns
