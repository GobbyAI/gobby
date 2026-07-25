"""Phase 5 Task dataclass contracts."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_is_escalated_field(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Escalation flag",
        validation_criteria="Test task completion is observable.",
    )

    temp_db.execute("UPDATE tasks SET is_escalated = TRUE WHERE id = %s", (task.id,))
    fetched = manager.get_task(task.id)

    assert fetched is not None
    assert hasattr(fetched, "is_escalated")
    assert fetched.is_escalated is True


def test_implementation_domain_persists(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Backend leaf",
        category="code",
        implementation_domain="backend",
        validation_criteria="Test task completion is observable.",
    )

    fetched = manager.get_task(task.id)

    assert fetched.implementation_domain == "backend"
    assert fetched.to_dict()["implementation_domain"] == "backend"
