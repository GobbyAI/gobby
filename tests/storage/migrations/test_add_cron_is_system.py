"""Red tests for the cron_jobs.is_system migration."""

from __future__ import annotations

import importlib

import pytest

from gobby.storage.database import LocalDatabase

pytestmark = pytest.mark.unit


def test_round_trip(tmp_path) -> None:
    migration = importlib.import_module("gobby.storage.migrations.add_cron_is_system")
    db = LocalDatabase(tmp_path / "cron-system.db")
    db.execute(
        """
        CREATE TABLE cron_jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL
        )
        """
    )
    db.execute("INSERT INTO cron_jobs (id, project_id, name) VALUES ('cj-1', 'p', 'dispatcher')")

    migration.up(db)
    row = db.fetchone("SELECT is_system FROM cron_jobs WHERE id = 'cj-1'")
    assert row["is_system"] == 0

    db.execute("UPDATE cron_jobs SET is_system = 1 WHERE id = 'cj-1'")
    migration.down(db)
    with pytest.raises(Exception, match="no such column: is_system"):
        db.fetchone("SELECT is_system FROM cron_jobs WHERE id = 'cj-1'")
    db.close()
