"""Migration 237 removes legacy review-round labels after manifest cutover."""

from __future__ import annotations

import json

import pytest

from gobby.storage.tasks import LocalTaskManager
from tests.phase5_contract_helpers import run_migration

pytestmark = pytest.mark.unit


def test_legacy_labels_dropped(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Legacy label cleanup",
        labels=[
            "keep",
            "planning-round:2",
            "qa-attempts:5",
            "owner:test",
        ],
    )

    run_migration(temp_db, 237)

    row = temp_db.fetchone("SELECT labels FROM tasks WHERE id = ?", (task.id,))
    assert json.loads(row["labels"]) == ["keep", "owner:test"]
