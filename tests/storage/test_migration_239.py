"""Tests for migration 239 zero-based stage position normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MIGRATIONS,
    _apply_baseline,
    _run_migration_list,
    get_current_version,
)
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _migration(version: int):
    for migration in MIGRATIONS:
        if migration[0] == version:
            return migration
    pytest.fail(f"migration {version} is missing from MIGRATIONS")


def _db_before_239(tmp_path: Path) -> LocalDatabase:
    db = LocalDatabase(tmp_path / "before-239.db")
    _apply_baseline(db)
    migrations = [item for item in MIGRATIONS if BASELINE_VERSION < item[0] < 239]
    _run_migration_list(db, BASELINE_VERSION, migrations)
    assert get_current_version(db) == 238
    return db


def _apply_239(db: LocalDatabase) -> None:
    _run_migration_list(db, get_current_version(db), [_migration(239)])


def _feature_task(db: LocalDatabase) -> str:
    project = LocalProjectManager(db).create(name="migration-239", repo_path="/tmp/m239")
    task = LocalTaskManager(db).create_task(
        project_id=project.id,
        title="Migration 239 feature",
        task_type="feature",
    )
    return task.id


def _positions(db: LocalDatabase, task_id: str) -> list[tuple[str, int]]:
    return [
        (row["stage_name"], row["position"])
        for row in db.fetchall(
            """
            SELECT stage_name, position
            FROM task_stage_states
            WHERE task_id = ?
            ORDER BY position, stage_name
            """,
            (task_id,),
        )
    ]


def test_migration_239_registered() -> None:
    assert _migration(239)[0] == 239


def test_default_stage_positions_are_dense_zero_based(tmp_path: Path) -> None:
    db = _db_before_239(tmp_path)

    _apply_239(db)

    task_types = {
        row["task_type"]
        for row in db.fetchall("SELECT DISTINCT task_type FROM task_type_default_stages")
    }
    for task_type in task_types:
        rows = db.fetchall(
            """
            SELECT position
            FROM task_type_default_stages
            WHERE task_type = ?
            ORDER BY position
            """,
            (task_type,),
        )
        assert [row["position"] for row in rows] == list(range(len(rows)))


def test_existing_task_stage_positions_are_dense_zero_based(tmp_path: Path) -> None:
    db = _db_before_239(tmp_path)
    task_id = _feature_task(db)

    _apply_239(db)

    rows = _positions(db, task_id)
    assert [position for _stage_name, position in rows] == list(range(len(rows)))


def test_task_stage_position_order_is_preserved(tmp_path: Path) -> None:
    db = _db_before_239(tmp_path)
    task_id = _feature_task(db)
    for stage_name, position in {
        "planning": 10,
        "expansion": 20,
        "test_arch": 30,
        "development": 40,
        "pr": 50,
        "merge": 60,
    }.items():
        db.execute(
            """
            UPDATE task_stage_states
               SET position = ?
             WHERE task_id = ? AND stage_name = ?
            """,
            (position, task_id, stage_name),
        )

    _apply_239(db)

    assert _positions(db, task_id) == [
        ("planning", 0),
        ("expansion", 1),
        ("test_arch", 2),
        ("development", 3),
        ("pr", 4),
        ("merge", 5),
    ]
