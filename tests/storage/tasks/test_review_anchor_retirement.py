"""Contracts for retiring the historical review-anchor task type."""

from __future__ import annotations

import psycopg
import pytest

from gobby.storage.migrations import _execute_sql_script
from gobby.storage.tasks import LocalTaskManager, StageRegistryManager
from tests.phase5_contract_helpers import repo_path

HISTORICAL_REVIEW_ANCHOR_REFS = (13984, 14853, 14928, 14931, 14933)


def test_migration_keeps_historical_review_anchor_tasks_readable(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    titles: dict[int, str] = {}

    for seq_num in HISTORICAL_REVIEW_ANCHOR_REFS:
        title = f"Historical review record #{seq_num}"
        task = manager.create_task(
            project_id=sample_project["id"],
            title=title,
            task_type="task",
            validation_criteria="Historical task remains readable.",
        )
        temp_db.execute(
            "UPDATE tasks SET seq_num = %s, task_type = %s WHERE id = %s",
            (seq_num, "review_anchor", task.id),
        )
        titles[seq_num] = title

    temp_db.execute(
        """
        INSERT INTO task_type_default_stages (task_type, stage_name, position)
        VALUES (%s, %s, %s)
        """,
        ("review_anchor", "planning", 0),
    )
    migration_path = repo_path("src/gobby/storage/migrations/361_retire_review_anchor.sql")

    with temp_db.transaction() as txn:
        _execute_sql_script(txn, migration_path.read_text(encoding="utf-8"))

    for seq_num, expected_title in titles.items():
        loaded = manager.get_task(f"#{seq_num}", project_id=sample_project["id"])
        assert loaded.task_type == "task"
        assert loaded.title == expected_title

    registry = StageRegistryManager(temp_db)
    assert registry.list_default_stages("review_anchor") == []


def test_migration_retypes_anchors_predating_validation_criteria(temp_db, sample_project) -> None:
    """Anchors older than migration 342 carry NULL criteria and must still retype.

    `tasks_require_validation_criteria` is NOT VALID, so those rows were never
    scanned — but retyping them re-checks the constraint for the updated row.
    """
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Anchor predating the validation-criteria requirement",
        task_type="task",
        validation_criteria="Placeholder replaced with NULL below.",
    )
    # Recreate history: the row predates migration 342, so it was written while
    # no constraint existed and then grandfathered by the NOT VALID addition.
    temp_db.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_require_validation_criteria")
    temp_db.execute(
        "UPDATE tasks SET task_type = %s, validation_criteria = NULL WHERE id = %s",
        ("review_anchor", task.id),
    )
    temp_db.execute(
        """
        ALTER TABLE tasks
        ADD CONSTRAINT tasks_require_validation_criteria
        CHECK (
            task_type = 'epic'
            OR NULLIF(BTRIM(validation_criteria), '') IS NOT NULL
        )
        NOT VALID
        """
    )
    migration_path = repo_path("src/gobby/storage/migrations/361_retire_review_anchor.sql")

    with temp_db.transaction() as txn:
        _execute_sql_script(txn, migration_path.read_text(encoding="utf-8"))

    loaded = manager.get_task(task.id)
    assert loaded.task_type == "task"
    assert loaded.validation_criteria is None

    # The constraint must survive the migration and still reject new violations.
    fresh = manager.create_task(
        project_id=sample_project["id"],
        title="Post-migration task",
        task_type="task",
        validation_criteria="Constraint still enforced after migration 361.",
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        temp_db.execute(
            "UPDATE tasks SET validation_criteria = NULL WHERE id = %s",
            (fresh.id,),
        )
