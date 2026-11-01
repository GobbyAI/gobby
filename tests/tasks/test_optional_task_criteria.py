"""Criteria requirements belong to selected policy, never structural task writes."""

from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


@pytest.fixture
def criteria_db(temp_db: HubDatabase) -> HubDatabase:
    constraint = temp_db.fetchone(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'tasks'::regclass "
        "AND conname = 'tasks_require_validation_criteria'"
    )
    assert constraint is None
    return temp_db


def test_storage_can_create_and_close_a_task_without_criteria(
    criteria_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    author = AgentSessions(criteria_db, sample_project["id"]).add("cake-maker")
    tasks = LocalTaskManager(criteria_db)
    task = tasks.create_task(sample_project["id"], "Bake a cake")
    assert task.validation_criteria is None

    tasks.close_task(task.id, closed_in_session_id=author)

    closed = tasks.get_task(task.id)
    assert closed.closed_at is not None
    assert closed.validation_criteria is None


def test_task_type_changes_do_not_create_criteria_requirements(
    criteria_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    tasks = LocalTaskManager(criteria_db)
    task = tasks.create_task(sample_project["id"], "A recipe", task_type="epic")

    updated = tasks.update_task(task.id, task_type="task")

    assert updated.task_type == "task"
    assert updated.validation_criteria is None


def test_schema_does_not_impose_a_workflow_on_task_metadata(
    criteria_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    tasks = LocalTaskManager(criteria_db)
    task = tasks.create_task(sample_project["id"], "A recipe", task_type="epic")

    criteria_db.execute("UPDATE tasks SET task_type = 'task' WHERE id = %s", (task.id,))

    stored = tasks.get_task(task.id)
    assert stored.task_type == "task"
    assert stored.validation_criteria is None


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["manual", "code"])
async def test_task_api_accepts_optional_workflow_metadata(
    criteria_db: HubDatabase, sample_project: dict[str, Any], category: str
) -> None:
    author = AgentSessions(criteria_db, sample_project["id"]).add("unrestricted-task-author")
    tasks = LocalTaskManager(criteria_db)
    with session_context_for_test(author):
        result = await create_task_registry(tasks).call(
            "create_task", {"title": "A deliverable", "category": category}
        )
    assert "error" not in result, result
    created = tasks.get_task(result["id"])
    assert created.validation_criteria is None
    assert created.implementation_domain is None
