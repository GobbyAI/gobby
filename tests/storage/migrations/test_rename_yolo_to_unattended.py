"""Red tests for the yolo to unattended storage migration."""

from __future__ import annotations

import importlib

import pytest

from gobby.storage.database import LocalDatabase

pytestmark = pytest.mark.unit


def test_round_trip(tmp_path) -> None:
    migration = importlib.import_module("gobby.storage.migrations.rename_yolo_to_unattended")
    temp_db = LocalDatabase(tmp_path / "rename.db")

    temp_db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, yolo INTEGER NOT NULL DEFAULT 0)")
    temp_db.execute("INSERT INTO tasks (id, yolo) VALUES ('task-1', 1)")

    migration.up(temp_db)
    row = temp_db.fetchone("SELECT unattended FROM tasks WHERE id = 'task-1'")

    assert row["unattended"] == 1
    with pytest.raises(Exception, match="no such column: yolo"):
        temp_db.fetchone("SELECT yolo FROM tasks WHERE id = 'task-1'")

    migration.down(temp_db)
    row = temp_db.fetchone("SELECT yolo FROM tasks WHERE id = 'task-1'")

    assert row["yolo"] == 1
    with pytest.raises(Exception, match="no such column: unattended"):
        temp_db.fetchone("SELECT unattended FROM tasks WHERE id = 'task-1'")
    temp_db.close()
