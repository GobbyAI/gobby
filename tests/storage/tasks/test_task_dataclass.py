"""Phase 5 Task dataclass contracts."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_is_escalated_field(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Escalation flag")

    temp_db.execute("UPDATE tasks SET is_escalated = 1 WHERE id = ?", (task.id,))
    fetched = manager.get_task(task.id)

    assert fetched is not None
    assert hasattr(fetched, "is_escalated")
    assert fetched.is_escalated is True

