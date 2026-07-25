"""Tests for storage layer GitHub field persistence.

Tests verify that LocalTaskManager correctly handles GitHub fields:
- create_task with GitHub field parameters
- update_task with GitHub field parameters
- Retrieval via get_task preserves GitHub fields

TDD Red Phase: Tests should fail initially until storage layer is updated.
"""

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def project_id(sample_project):
    return sample_project["id"]


class TestTaskManagerGitHubCreate:
    """Test creating tasks with GitHub fields."""

    def test_create_task_with_github_issue_number(self, task_manager, project_id) -> None:
        """create_task should accept github_issue_number parameter."""
        task = task_manager.create_task(
            project_id=project_id,
            title="Issue-linked task",
            github_issue_number=42,
            validation_criteria="Test task completion is observable.",
        )
        assert task.github_issue_number == 42

    def test_create_task_with_github_pr_number(self, task_manager, project_id) -> None:
        """create_task should accept github_pr_number parameter."""
        task = task_manager.create_task(
            project_id=project_id,
            title="PR-linked task",
            github_pr_number=123,
            validation_criteria="Test task completion is observable.",
        )
        assert task.github_pr_number == 123

    def test_create_task_with_github_repo(self, task_manager, project_id) -> None:
        """create_task should accept github_repo parameter."""
        task = task_manager.create_task(
            project_id=project_id,
            title="Repo-linked task",
            github_repo="owner/repo",
            validation_criteria="Test task completion is observable.",
        )
        assert task.github_repo == "owner/repo"

    def test_create_task_with_all_github_fields(self, task_manager, project_id) -> None:
        """create_task should accept all GitHub fields together."""
        task = task_manager.create_task(
            project_id=project_id,
            title="Fully linked task",
            github_issue_number=100,
            github_pr_number=200,
            github_repo="myorg/myrepo",
            validation_criteria="Test task completion is observable.",
        )
        assert task.github_issue_number == 100
        assert task.github_pr_number == 200
        assert task.github_repo == "myorg/myrepo"

    def test_create_task_github_fields_persist_to_db(self, task_manager, project_id) -> None:
        """GitHub fields should persist to database and be retrievable."""
        created = task_manager.create_task(
            project_id=project_id,
            title="Persistent task",
            github_issue_number=55,
            github_pr_number=66,
            github_repo="test/repo",
            validation_criteria="Test task completion is observable.",
        )

        # Retrieve fresh from database
        fetched = task_manager.get_task(created.id)
        assert fetched.github_issue_number == 55
        assert fetched.github_pr_number == 66
        assert fetched.github_repo == "test/repo"


class TestTaskManagerGitHubUpdate:
    """Test updating tasks with GitHub fields."""

    def test_update_task_github_issue_number(self, task_manager, project_id) -> None:
        """update_task should accept github_issue_number parameter."""
        task = task_manager.create_task(
            project_id=project_id,
            title="To update",
            validation_criteria="Test task completion is observable.",
        )
        updated = task_manager.update_task(task.id, github_issue_number=99)
        assert updated.github_issue_number == 99

    def test_update_task_github_pr_number(self, task_manager, project_id) -> None:
        """update_task should accept github_pr_number parameter."""
        task = task_manager.create_task(
            project_id=project_id,
            title="To update",
            validation_criteria="Test task completion is observable.",
        )
        updated = task_manager.update_task(task.id, github_pr_number=88)
        assert updated.github_pr_number == 88

    def test_update_task_github_repo(self, task_manager, project_id) -> None:
        """update_task should accept github_repo parameter."""
        task = task_manager.create_task(
            project_id=project_id,
            title="To update",
            validation_criteria="Test task completion is observable.",
        )
        updated = task_manager.update_task(task.id, github_repo="new/repo")
        assert updated.github_repo == "new/repo"

    def test_update_task_all_github_fields(self, task_manager, project_id) -> None:
        """update_task should accept all GitHub fields together."""
        task = task_manager.create_task(
            project_id=project_id,
            title="To update",
            validation_criteria="Test task completion is observable.",
        )
        updated = task_manager.update_task(
            task.id,
            github_issue_number=111,
            github_pr_number=222,
            github_repo="updated/repo",
        )
        assert updated.github_issue_number == 111
        assert updated.github_pr_number == 222
        assert updated.github_repo == "updated/repo"

    def test_update_task_clear_github_fields(self, task_manager, project_id) -> None:
        """update_task should allow clearing GitHub fields by setting to None."""
        task = task_manager.create_task(
            project_id=project_id,
            title="To clear",
            github_issue_number=50,
            github_repo="some/repo",
            validation_criteria="Test task completion is observable.",
        )

        # Clear the fields
        updated = task_manager.update_task(
            task.id,
            github_issue_number=None,
            github_repo=None,
        )
        assert updated.github_issue_number is None
        assert updated.github_repo is None

    def test_update_task_github_fields_persist(self, task_manager, project_id) -> None:
        """Updated GitHub fields should persist to database."""
        task = task_manager.create_task(
            project_id=project_id,
            title="Persist test",
            validation_criteria="Test task completion is observable.",
        )
        task_manager.update_task(
            task.id,
            github_issue_number=77,
            github_pr_number=88,
            github_repo="persist/test",
        )

        # Retrieve fresh from database
        fetched = task_manager.get_task(task.id)
        assert fetched.github_issue_number == 77
        assert fetched.github_pr_number == 88
        assert fetched.github_repo == "persist/test"


class TestTaskManagerGitHubList:
    """Test listing tasks with GitHub fields."""

    def test_list_tasks_includes_github_fields(self, task_manager, project_id) -> None:
        """list_tasks should return tasks with GitHub fields populated."""
        task_manager.create_task(
            project_id=project_id,
            title="Listed task",
            github_issue_number=30,
            github_repo="list/test",
            validation_criteria="Test task completion is observable.",
        )

        tasks = task_manager.list_tasks(project_id=project_id)
        assert len(tasks) == 1
        assert tasks[0].github_issue_number == 30
        assert tasks[0].github_repo == "list/test"
