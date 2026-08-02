"""Contracts for retiring the historical review-anchor task type."""

from __future__ import annotations

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
