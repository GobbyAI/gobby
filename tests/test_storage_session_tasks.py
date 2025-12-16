import pytest
from unittest.mock import patch, MagicMock
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.sessions import LocalSessionManager


@pytest.fixture
def session_task_manager(temp_db):
    return SessionTaskManager(temp_db)


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def session_manager(temp_db):
    return LocalSessionManager(temp_db)


@pytest.fixture
def sample_task(task_manager, sample_project):
    return task_manager.create_task(project_id=sample_project["id"], title="Test Task")


@pytest.fixture
def sample_session(session_manager, sample_project):
    return session_manager.register(
        external_id="ext-123",
        machine_id="machine-1",
        source="cli",
        project_id=sample_project["id"],
        title="Sample Session",
    )


class TestSessionTaskManager:
    def test_link_task(self, session_task_manager, sample_task, sample_session):
        session_id = sample_session.id
        session_task_manager.link_task(session_id, sample_task.id, "worked_on")

        tasks = session_task_manager.get_session_tasks(session_id)
        assert len(tasks) == 1
        assert tasks[0]["task"].id == sample_task.id
        assert tasks[0]["action"] == "worked_on"

    def test_link_duplicate_ignored(self, session_task_manager, sample_task, sample_session):
        session_id = sample_session.id
        session_task_manager.link_task(session_id, sample_task.id, "worked_on")
        session_task_manager.link_task(session_id, sample_task.id, "worked_on")

        tasks = session_task_manager.get_session_tasks(session_id)
        assert len(tasks) == 1

    def test_link_invalid_action(self, session_task_manager, sample_task, sample_session):
        session_id = sample_session.id
        with pytest.raises(ValueError, match="Invalid action"):
            session_task_manager.link_task(session_id, sample_task.id, "invalid_action")

    def test_unlink_task(self, session_task_manager, sample_task, sample_session):
        session_id = sample_session.id
        session_task_manager.link_task(session_id, sample_task.id, "worked_on")
        session_task_manager.unlink_task(session_id, sample_task.id, "worked_on")

        tasks = session_task_manager.get_session_tasks(session_id)
        assert len(tasks) == 0

    def test_get_task_sessions(
        self, session_task_manager, sample_task, session_manager, sample_project
    ):
        # Create two distinct sessions
        s1 = session_manager.register(
            external_id="ext-1", machine_id="m1", source="s1", project_id=sample_project["id"]
        )
        s2 = session_manager.register(
            external_id="ext-2", machine_id="m1", source="s1", project_id=sample_project["id"]
        )

        session_task_manager.link_task(s1.id, sample_task.id, "worked_on")
        session_task_manager.link_task(s2.id, sample_task.id, "mentioned")

        sessions = session_task_manager.get_task_sessions(sample_task.id)
        assert len(sessions) == 2

        # Verify actions and session IDs
        found_links = {(s["session_id"], s["action"]) for s in sessions}
        assert (s1.id, "worked_on") in found_links
        assert (s2.id, "mentioned") in found_links

    def test_multiple_actions_same_session(self, session_task_manager, sample_task, sample_session):
        session_id = sample_session.id
        # A task can be both mentioned and worked on in the same session
        session_task_manager.link_task(session_id, sample_task.id, "mentioned")
        session_task_manager.link_task(session_id, sample_task.id, "worked_on")

        tasks = session_task_manager.get_session_tasks(session_id)
        assert len(tasks) == 2
        actions = {t["action"] for t in tasks}
        assert "mentioned" in actions
        assert "worked_on" in actions
