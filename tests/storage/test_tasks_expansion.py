import uuid

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit

# projects.id is a native uuid column.
PROJECT_ID = str(uuid.uuid4())


@pytest.fixture
def db(temp_db):
    database = temp_db
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "test_project")
        )
    return database


@pytest.fixture
def manager(db):
    return LocalTaskManager(db)


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.e2e
def test_create_task_preserves_description_and_category(manager):
    task = manager.create_task(
        project_id=PROJECT_ID,
        title="Test Expansion",
        description="Rich details here",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )

    assert task.description == "Rich details here"
    assert task.category == "planning"

    fetched = manager.get_task(task.id)
    assert fetched.description == "Rich details here"
    assert fetched.category == "planning"


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.e2e
def test_update_task_description(manager):
    task = manager.create_task(
        project_id=PROJECT_ID,
        title="Update Me",
        validation_criteria="Test task completion is observable.",
    )
    updated = manager.update_task(task.id, description="Updated details")
    assert updated.description == "Updated details"

    fetched = manager.get_task(task.id)
    assert fetched.description == "Updated details"


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.e2e
def test_to_dict_excludes_legacy_expansion_fields(manager):
    task = manager.create_task(
        project_id=PROJECT_ID,
        title="Dict Test",
        description="Secret details",
        validation_criteria="Test task completion is observable.",
    )

    data = task.to_dict()
    assert data["description"] == "Secret details"
    assert "expansion_context" not in data
    assert "expansion_status" not in data
