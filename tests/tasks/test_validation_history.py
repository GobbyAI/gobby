"""Tests for ValidationHistoryManager."""

import json
from uuid import uuid4

import pytest

from gobby.failure_categories import FailureCategory
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.validation_history import (
    ValidationHistoryManager,
    ValidationIteration,
    _safe_failure_category,
)
from gobby.tasks.validation_models import Issue, IssueSeverity, IssueType

pytestmark = pytest.mark.unit


def test_unknown_stored_failure_category_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    assert _safe_failure_category("future-category") is None
    assert "Ignoring unknown validation failure category" in caplog.text


@pytest.fixture
def history_manager(temp_db: HubDatabase) -> ValidationHistoryManager:
    """Create a ValidationHistoryManager with test database."""
    return ValidationHistoryManager(temp_db)


@pytest.fixture
def sample_project(temp_db: HubDatabase) -> dict[str, str]:
    """Create a sample project for tests."""
    project_id = str(uuid4())
    temp_db.execute(
        """INSERT INTO projects (id, name, created_at, updated_at)
           VALUES (%s, %s, NOW(), NOW())""",
        (project_id, "Test Project"),
    )
    return {"id": project_id, "name": "Test Project"}


@pytest.fixture
def sample_task(temp_db: HubDatabase, sample_project: dict[str, str]) -> dict[str, str]:
    """Create a sample task for tests."""
    task_id = str(uuid4())
    temp_db.execute(
        """INSERT INTO tasks (
               id, project_id, title, priority, task_type, validation_criteria,
               created_at, updated_at
           )
           VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())""",
        (
            task_id,
            sample_project["id"],
            "Test Task",
            2,
            "task",
            "Validation history behavior is observable.",
        ),
    )
    return {"id": task_id}


def _seed_iteration(
    db: HubDatabase,
    *,
    task_id: str,
    iteration: int,
    status: str,
    feedback: str | None = None,
    issues: list[Issue] | None = None,
    context_type: str | None = None,
    context_summary: str | None = None,
    validator_type: str | None = None,
    failure_category: FailureCategory | None = None,
) -> None:
    """Represent rows archived before automatic validation was retired."""
    db.execute(
        """INSERT INTO task_validation_history (
            task_id, iteration, status, feedback, issues, context_type,
            context_summary, validator_type, failure_category
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            task_id,
            iteration,
            status,
            feedback,
            json.dumps([issue.to_dict() for issue in issues]) if issues is not None else None,
            context_type,
            context_summary,
            validator_type,
            failure_category.value if failure_category else None,
        ),
    )


class TestValidationHistoryManager:
    """Tests for ValidationHistoryManager class."""

    def test_read_archived_iteration_fields(
        self, history_manager: ValidationHistoryManager, sample_task: dict[str, str]
    ) -> None:
        """Read an archived row without changing its recorded outcome."""
        issues = [
            Issue(
                issue_type=IssueType.TEST_FAILURE,
                severity=IssueSeverity.MAJOR,
                title="Test failed",
            )
        ]

        _seed_iteration(
            history_manager.db,
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            feedback="Tests are failing",
            issues=issues,
            context_type="git_diff",
            context_summary="Modified 3 files",
            validator_type="llm",
            failure_category=FailureCategory.TEST,
        )

        # Verify record exists in database
        history = history_manager.get_iteration_history(sample_task["id"])
        assert len(history) == 1
        assert history[0].iteration == 1
        assert history[0].status == "invalid"
        assert history[0].feedback == "Tests are failing"
        assert history[0].failure_category is FailureCategory.TEST

    def test_read_archived_iterations_in_order(
        self, history_manager: ValidationHistoryManager, sample_task: dict[str, str]
    ) -> None:
        """Read all archived iterations in their original order."""
        for i in range(1, 4):
            _seed_iteration(
                history_manager.db,
                task_id=sample_task["id"],
                iteration=i,
                status="invalid" if i < 3 else "valid",
                feedback=f"Iteration {i}",
            )

        history = history_manager.get_iteration_history(sample_task["id"])
        assert len(history) == 3
        assert history[0].iteration == 1
        assert history[1].iteration == 2
        assert history[2].iteration == 3

    def test_get_iteration_history_returns_all_iterations(
        self, history_manager: ValidationHistoryManager, sample_task: dict[str, str]
    ) -> None:
        """Test that get_iteration_history returns all iterations for a task."""
        # Record multiple iterations with different data
        _seed_iteration(
            history_manager.db,
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            feedback="First attempt",
            validator_type="llm",
        )
        _seed_iteration(
            history_manager.db,
            task_id=sample_task["id"],
            iteration=2,
            status="valid",
            feedback="Fixed",
            validator_type="llm",
        )

        history = history_manager.get_iteration_history(sample_task["id"])

        assert len(history) == 2
        assert all(isinstance(item, ValidationIteration) for item in history)

    def test_history_includes_issues(
        self, history_manager: ValidationHistoryManager, sample_task: dict[str, str]
    ) -> None:
        """Test that history includes serialized issues."""
        issues = [
            Issue(
                issue_type=IssueType.TEST_FAILURE,
                severity=IssueSeverity.BLOCKER,
                title="Critical test failure",
                location="tests/test_core.py:42",
            ),
            Issue(
                issue_type=IssueType.LINT_ERROR,
                severity=IssueSeverity.MINOR,
                title="Unused import",
            ),
        ]

        _seed_iteration(
            history_manager.db,
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            issues=issues,
        )

        history = history_manager.get_iteration_history(sample_task["id"])
        assert history[0].issues is not None
        assert len(history[0].issues) == 2
        assert history[0].issues[0].title == "Critical test failure"
        assert history[0].issues[1].title == "Unused import"

    def test_history_includes_context(
        self, history_manager: ValidationHistoryManager, sample_task: dict[str, str]
    ) -> None:
        """Test that history includes context information."""
        _seed_iteration(
            history_manager.db,
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            context_type="git_diff",
            context_summary="Changed auth.py: +50/-20 lines",
        )

        history = history_manager.get_iteration_history(sample_task["id"])
        assert history[0].context_type == "git_diff"
        assert history[0].context_summary == "Changed auth.py: +50/-20 lines"

    def test_history_includes_validator_type(
        self, history_manager: ValidationHistoryManager, sample_task: dict[str, str]
    ) -> None:
        """Test that history includes validator type."""
        _seed_iteration(
            history_manager.db,
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            validator_type="external_webhook",
        )

        history = history_manager.get_iteration_history(sample_task["id"])
        assert history[0].validator_type == "external_webhook"

    def test_get_latest_iteration(
        self, history_manager: ValidationHistoryManager, sample_task: dict[str, str]
    ) -> None:
        """Test getting the latest iteration for a task."""
        _seed_iteration(
            history_manager.db, task_id=sample_task["id"], iteration=1, status="invalid"
        )
        _seed_iteration(
            history_manager.db, task_id=sample_task["id"], iteration=2, status="invalid"
        )
        _seed_iteration(history_manager.db, task_id=sample_task["id"], iteration=3, status="valid")

        latest = history_manager.get_latest_iteration(sample_task["id"])

        assert latest is not None
        assert latest.iteration == 3
        assert latest.status == "valid"

    def test_get_latest_iteration_uses_newest_id_to_break_iteration_ties(
        self, history_manager: ValidationHistoryManager, sample_task: dict[str, str]
    ) -> None:
        _seed_iteration(
            history_manager.db, task_id=sample_task["id"], iteration=3, status="invalid"
        )
        _seed_iteration(history_manager.db, task_id=sample_task["id"], iteration=3, status="valid")

        latest = history_manager.get_latest_iteration(sample_task["id"])
        history = history_manager.get_iteration_history(sample_task["id"])

        assert latest is not None
        assert latest.status == "valid"
        assert [item.status for item in history] == ["invalid", "valid"]

    def test_get_latest_iteration_empty_history(
        self, history_manager: ValidationHistoryManager, sample_task: dict[str, str]
    ) -> None:
        """Test get_latest_iteration returns None for empty history."""
        latest = history_manager.get_latest_iteration(sample_task["id"])
        assert latest is None

    def test_validation_iteration_dataclass(
        self, history_manager: ValidationHistoryManager, sample_task: dict[str, str]
    ) -> None:
        """Test ValidationIteration dataclass structure."""
        issues = [
            Issue(
                issue_type=IssueType.SECURITY,
                severity=IssueSeverity.BLOCKER,
                title="SQL injection",
            )
        ]

        _seed_iteration(
            history_manager.db,
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            feedback="Security issue found",
            issues=issues,
            context_type="code_review",
            context_summary="Reviewed db.py",
            validator_type="llm",
        )

        history = history_manager.get_iteration_history(sample_task["id"])
        iteration = history[0]

        # Check all expected fields exist
        assert hasattr(iteration, "id")
        assert hasattr(iteration, "task_id")
        assert hasattr(iteration, "iteration")
        assert hasattr(iteration, "status")
        assert hasattr(iteration, "feedback")
        assert hasattr(iteration, "issues")
        assert hasattr(iteration, "context_type")
        assert hasattr(iteration, "context_summary")
        assert hasattr(iteration, "validator_type")
        assert hasattr(iteration, "created_at")

        assert iteration.task_id == sample_task["id"]
        assert iteration.status == "invalid"
