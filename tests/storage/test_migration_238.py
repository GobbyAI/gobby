"""Tests for migration 238 registry policy repair."""

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


def _db_before_238(tmp_path: Path) -> LocalDatabase:
    db = LocalDatabase(tmp_path / "before-238.db")
    _apply_baseline(db)
    migrations = [item for item in MIGRATIONS if BASELINE_VERSION < item[0] < 238]
    _run_migration_list(db, BASELINE_VERSION, migrations)
    assert get_current_version(db) == 237
    return db


def _apply_238(db: LocalDatabase) -> None:
    _run_migration_list(db, get_current_version(db), [_migration(238)])


def _feature_task(db: LocalDatabase) -> str:
    project = LocalProjectManager(db).create(name="migration-238", repo_path="/tmp/m238")
    task = LocalTaskManager(db).create_task(
        project_id=project.id,
        title="Migration 238 feature",
        task_type="feature",
    )
    return task.id


def test_migration_238_registered() -> None:
    assert _migration(238)[0] == 238


def test_repairs_registry_policy_from_bundled_yaml(tmp_path: Path) -> None:
    db = _db_before_238(tmp_path)
    db.execute(
        """
        UPDATE task_stages_registry
           SET review_policy = 'none',
               reviewer_agent = NULL,
               bundled_hash = 'old-hash'
         WHERE name = 'planning'
        """
    )

    _apply_238(db)

    row = db.fetchone(
        """
        SELECT review_policy, reviewer_agent, bundled_hash
        FROM task_stages_registry
        WHERE name = 'planning'
        """
    )
    assert row["review_policy"] == "required"
    assert row["reviewer_agent"] == "plan-adversary"
    assert row["bundled_hash"] != "old-hash"


def test_backfills_existing_task_stage_rows(tmp_path: Path) -> None:
    db = _db_before_238(tmp_path)
    task_id = _feature_task(db)
    db.execute(
        """
        UPDATE task_stage_states
           SET review_policy = 'none',
               reviewer_agent = NULL
         WHERE task_id = ? AND stage_name = 'development'
        """,
        (task_id,),
    )

    _apply_238(db)

    row = db.fetchone(
        """
        SELECT review_policy, reviewer_agent
        FROM task_stage_states
        WHERE task_id = ? AND stage_name = 'development'
        """,
        (task_id,),
    )
    assert row["review_policy"] == "required"
    assert row["reviewer_agent"] == "qa-reviewer"


def test_user_added_stage_is_preserved(tmp_path: Path) -> None:
    db = _db_before_238(tmp_path)
    db.execute(
        """
        INSERT INTO task_stages_registry (
            name, display_label, description, category, review_policy,
            position_hint, requires_human, is_terminal, bundled_hash, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            "operator_review",
            "Operator Review",
            "Local operator review",
            "verification",
            "optional",
            999,
            1,
            0,
            None,
        ),
    )

    _apply_238(db)

    row = db.fetchone(
        "SELECT review_policy FROM task_stages_registry WHERE name = 'operator_review'"
    )
    assert row["review_policy"] == "optional"
