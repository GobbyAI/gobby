import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.tasks import LocalTaskManager
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def db(db_path):
    database = LocalDatabase(str(db_path))
    run_migrations(database)
    with database.transaction() as conn:
        conn.execute("INSERT INTO projects (id, name) VALUES (?, ?)", ("p1", "test_project"))
    return database


@pytest.fixture
def manager(db):
    return LocalTaskManager(db)


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.e2e
def test_create_task_preserves_description_and_category(manager):
    task = manager.create_task(
        project_id="p1",
        title="Test Expansion",
        description="Rich details here",
        category="planning",
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
    task = manager.create_task(project_id="p1", title="Update Me")
    updated = manager.update_task(task.id, description="Updated details")
    assert updated.description == "Updated details"

    fetched = manager.get_task(task.id)
    assert fetched.description == "Updated details"


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.e2e
def test_to_dict_excludes_legacy_expansion_fields(manager):
    task = manager.create_task(project_id="p1", title="Dict Test", description="Secret details")

    data = task.to_dict()
    assert data["description"] == "Secret details"
    assert "expansion_context" not in data
    assert "expansion_status" not in data
