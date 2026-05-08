"""Tests for GitHub integration fields on Task dataclass.

These tests verify that the Task dataclass includes:
- github_issue_number (Optional[int])
- github_pr_number (Optional[int])
- github_repo (Optional[str])

TDD Red Phase: Tests should fail initially until fields are implemented.
"""

import pytest

from gobby.storage.tasks import LocalTaskManager, Task

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def project_id(sample_project):
    return sample_project["id"]


class TestTaskGitHubFields:
    """Test GitHub integration fields on Task dataclass."""

    def test_task_has_github_issue_number_field(self) -> None:
        """Task dataclass should have github_issue_number field."""
        # Check that the field exists on the dataclass
        assert hasattr(Task, "__dataclass_fields__")
        assert "github_issue_number" in Task.__dataclass_fields__

    def test_task_has_github_pr_number_field(self) -> None:
        """Task dataclass should have github_pr_number field."""
        assert hasattr(Task, "__dataclass_fields__")
        assert "github_pr_number" in Task.__dataclass_fields__

    def test_task_has_github_repo_field(self) -> None:
        """Task dataclass should have github_repo field."""
        assert hasattr(Task, "__dataclass_fields__")
        assert "github_repo" in Task.__dataclass_fields__

    def test_github_fields_default_to_none(self, task_manager, project_id) -> None:
        """GitHub fields should default to None when creating a task."""
        task = task_manager.create_task(
            project_id=project_id,
            title="Test task",
        )
        assert task.github_issue_number is None
        assert task.github_pr_number is None
        assert task.github_repo is None

    def test_github_fields_in_to_dict(self, task_manager, project_id) -> None:
        """GitHub fields should appear in to_dict() output."""
        task = task_manager.create_task(
            project_id=project_id,
            title="Test task",
        )
        task_dict = task.to_dict()
        assert "github_issue_number" in task_dict
        assert "github_pr_number" in task_dict
        assert "github_repo" in task_dict

    def test_github_fields_roundtrip(self, temp_db, project_id) -> None:
        """GitHub fields should survive database roundtrip via from_row()."""
        # Create a task with GitHub fields set via direct SQL
        # (since create_task doesn't support these fields yet)
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()

        with temp_db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, priority, task_type,
                    created_at, updated_at, github_issue_number, github_pr_number, github_repo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "gt-test01",
                    project_id,
                    "GitHub linked task",
                    2,
                    "task",
                    now,
                    now,
                    123,  # github_issue_number
                    456,  # github_pr_number
                    "owner/repo",  # github_repo
                ),
            )

        # Fetch and verify via Task.from_row
        row = temp_db.fetchone("SELECT * FROM tasks WHERE id = ?", ("gt-test01",))
        task = Task.from_row(row)

        assert task.github_issue_number == 123
        assert task.github_pr_number == 456
        assert task.github_repo == "owner/repo"


class TestTaskGitHubFieldTypes:
    """Test type handling for GitHub fields."""

    def test_github_issue_number_is_optional_int(self) -> None:
        """github_issue_number should be Optional[int]."""
        field = Task.__dataclass_fields__["github_issue_number"]
        # Default should be None (making it optional)
        assert field.default is None

    def test_github_pr_number_is_optional_int(self) -> None:
        """github_pr_number should be Optional[int]."""
        field = Task.__dataclass_fields__["github_pr_number"]
        assert field.default is None

    def test_github_repo_is_optional_str(self) -> None:
        """github_repo should be Optional[str]."""
        field = Task.__dataclass_fields__["github_repo"]
        assert field.default is None
