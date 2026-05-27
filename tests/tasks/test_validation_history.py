"""Tests for ValidationHistoryManager."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from gobby.tasks.validation_history import ValidationHistoryManager, ValidationIteration
from gobby.tasks.validation_models import Issue, IssueSeverity, IssueType

pytestmark = pytest.mark.unit


@pytest.fixture
def history_manager(temp_db):
    """Create a ValidationHistoryManager with test database."""
    return ValidationHistoryManager(temp_db)


@pytest.fixture
def sample_project(temp_db):
    """Create a sample project for tests."""
    temp_db.execute(
        """INSERT INTO projects (id, name, created_at, updated_at)
           VALUES (%s, %s, NOW(), NOW())""",
        ("test-project", "Test Project"),
    )
    return {"id": "test-project", "name": "Test Project"}


@pytest.fixture
def sample_task(temp_db, sample_project):
    """Create a sample task for tests."""
    temp_db.execute(
        """INSERT INTO tasks (id, project_id, title, priority, task_type, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, NOW(), NOW())""",
        ("gt-test123", sample_project["id"], "Test Task", 2, "task"),
    )
    return {"id": "gt-test123"}


class TestValidationHistoryManager:
    """Tests for ValidationHistoryManager class."""

    def test_record_iteration_stores_data(self, history_manager, sample_task) -> None:
        """Test that record_iteration stores iteration data in database."""
        issues = [
            Issue(
                issue_type=IssueType.TEST_FAILURE,
                severity=IssueSeverity.MAJOR,
                title="Test failed",
            )
        ]

        history_manager.record_iteration(
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            feedback="Tests are failing",
            issues=issues,
            context_type="git_diff",
            context_summary="Modified 3 files",
            validator_type="llm",
        )

        # Verify record exists in database
        history = history_manager.get_iteration_history(sample_task["id"])
        assert len(history) == 1
        assert history[0].iteration == 1
        assert history[0].status == "invalid"
        assert history[0].feedback == "Tests are failing"

    def test_record_multiple_iterations(self, history_manager, sample_task) -> None:
        """Test recording multiple iterations for same task."""
        for i in range(1, 4):
            history_manager.record_iteration(
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
        self, history_manager, sample_task
    ) -> None:
        """Test that get_iteration_history returns all iterations for a task."""
        # Record multiple iterations with different data
        history_manager.record_iteration(
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            feedback="First attempt",
            validator_type="llm",
        )
        history_manager.record_iteration(
            task_id=sample_task["id"],
            iteration=2,
            status="valid",
            feedback="Fixed",
            validator_type="llm",
        )

        history = history_manager.get_iteration_history(sample_task["id"])

        assert len(history) == 2
        assert all(isinstance(item, ValidationIteration) for item in history)

    def test_history_includes_issues(self, history_manager, sample_task) -> None:
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

        history_manager.record_iteration(
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            issues=issues,
        )

        history = history_manager.get_iteration_history(sample_task["id"])
        assert len(history[0].issues) == 2
        assert history[0].issues[0].title == "Critical test failure"
        assert history[0].issues[1].title == "Unused import"

    def test_history_includes_context(self, history_manager, sample_task) -> None:
        """Test that history includes context information."""
        history_manager.record_iteration(
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            context_type="git_diff",
            context_summary="Changed auth.py: +50/-20 lines",
        )

        history = history_manager.get_iteration_history(sample_task["id"])
        assert history[0].context_type == "git_diff"
        assert history[0].context_summary == "Changed auth.py: +50/-20 lines"

    def test_history_includes_validator_type(self, history_manager, sample_task) -> None:
        """Test that history includes validator type."""
        history_manager.record_iteration(
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            validator_type="external_webhook",
        )

        history = history_manager.get_iteration_history(sample_task["id"])
        assert history[0].validator_type == "external_webhook"

    def test_clear_history_removes_all_iterations(self, history_manager, sample_task) -> None:
        """Test that clear_history removes all iterations for a task."""
        # Add several iterations
        for i in range(1, 4):
            history_manager.record_iteration(
                task_id=sample_task["id"],
                iteration=i,
                status="invalid",
            )

        # Verify they exist
        assert len(history_manager.get_iteration_history(sample_task["id"])) == 3

        # Clear history
        history_manager.clear_history(sample_task["id"])

        # Verify they're gone
        assert len(history_manager.get_iteration_history(sample_task["id"])) == 0

    def test_clear_history_only_affects_target_task(
        self, history_manager, temp_db, sample_project, sample_task
    ) -> None:
        """Test that clear_history only affects the target task."""
        # Create a second task
        temp_db.execute(
            """INSERT INTO tasks (id, project_id, title, priority, task_type, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, NOW(), NOW())""",
            ("gt-other", sample_project["id"], "Other Task", 2, "task"),
        )

        # Add history to both tasks
        history_manager.record_iteration(task_id=sample_task["id"], iteration=1, status="invalid")
        history_manager.record_iteration(task_id="gt-other", iteration=1, status="invalid")

        # Clear only first task
        history_manager.clear_history(sample_task["id"])

        # First task should have no history
        assert len(history_manager.get_iteration_history(sample_task["id"])) == 0
        # Second task should still have history
        assert len(history_manager.get_iteration_history("gt-other")) == 1

    def test_get_latest_iteration(self, history_manager, sample_task) -> None:
        """Test getting the latest iteration for a task."""
        history_manager.record_iteration(task_id=sample_task["id"], iteration=1, status="invalid")
        history_manager.record_iteration(task_id=sample_task["id"], iteration=2, status="invalid")
        history_manager.record_iteration(task_id=sample_task["id"], iteration=3, status="valid")

        latest = history_manager.get_latest_iteration(sample_task["id"])

        assert latest is not None
        assert latest.iteration == 3
        assert latest.status == "valid"

    def test_get_latest_iteration_empty_history(self, history_manager, sample_task) -> None:
        """Test get_latest_iteration returns None for empty history."""
        latest = history_manager.get_latest_iteration(sample_task["id"])
        assert latest is None

    def test_concurrent_iteration_recording(self, history_manager, sample_task) -> None:
        """Test that concurrent iteration recording is safe."""

        def record_iteration(iteration_num):
            history_manager.record_iteration(
                task_id=sample_task["id"],
                iteration=iteration_num,
                status="invalid",
                feedback=f"Concurrent {iteration_num}",
            )

        # Record 10 iterations concurrently
        with ThreadPoolExecutor(max_workers=5) as executor:
            executor.map(record_iteration, range(1, 11))

        # All should be recorded
        history = history_manager.get_iteration_history(sample_task["id"])
        assert len(history) == 10

    def test_validation_iteration_dataclass(self, history_manager, sample_task) -> None:
        """Test ValidationIteration dataclass structure."""
        issues = [
            Issue(
                issue_type=IssueType.SECURITY,
                severity=IssueSeverity.BLOCKER,
                title="SQL injection",
            )
        ]

        history_manager.record_iteration(
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


class TestRecurringIssueDetection:
    """Tests for recurring issue detection functionality."""

    @pytest.fixture
    def history_manager(self, temp_db):
        """Create a ValidationHistoryManager with test database."""
        return ValidationHistoryManager(temp_db)

    @pytest.fixture
    def sample_project(self, temp_db):
        """Create a sample project for tests."""
        temp_db.execute(
            """INSERT INTO projects (id, name, created_at, updated_at)
               VALUES (%s, %s, NOW(), NOW())""",
            ("test-project", "Test Project"),
        )
        return {"id": "test-project"}

    @pytest.fixture
    def sample_task(self, temp_db, sample_project):
        """Create a sample task for tests."""
        temp_db.execute(
            """INSERT INTO tasks (id, project_id, title, priority, task_type, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, NOW(), NOW())""",
            ("gt-recurring", sample_project["id"], "Test Task", 2, "task"),
        )
        return {"id": "gt-recurring"}

    def test_group_similar_issues_clusters_by_title(self, history_manager) -> None:
        """Test that group_similar_issues clusters issues with similar titles."""
        issues = [
            Issue(IssueType.TEST_FAILURE, IssueSeverity.MAJOR, "Test auth failed"),
            Issue(IssueType.TEST_FAILURE, IssueSeverity.MAJOR, "Test auth failed"),
            Issue(IssueType.TEST_FAILURE, IssueSeverity.MAJOR, "Test auth fails"),  # Similar
            Issue(IssueType.LINT_ERROR, IssueSeverity.MINOR, "Unused import"),
        ]

        groups = history_manager.group_similar_issues(issues)

        # Should have 2 groups: auth failures and lint error
        assert len(groups) == 2
        # The auth group should have 3 issues
        auth_group = [g for g in groups if "auth" in g[0].title.lower()][0]
        assert len(auth_group) >= 2

    def test_group_similar_issues_respects_threshold(self, history_manager) -> None:
        """Test that fuzzy matching respects similarity threshold."""
        issues = [
            Issue(IssueType.TEST_FAILURE, IssueSeverity.MAJOR, "Test authentication failed"),
            Issue(
                IssueType.TEST_FAILURE, IssueSeverity.MAJOR, "Test authorization failed"
            ),  # Different
        ]

        # With high threshold, these should be separate groups
        groups = history_manager.group_similar_issues(issues, similarity_threshold=0.9)
        assert len(groups) == 2

        # With lower threshold, they might be grouped
        groups_low = history_manager.group_similar_issues(issues, similarity_threshold=0.5)
        assert len(groups_low) <= 2

    def test_group_similar_issues_same_location_strong_match(self, history_manager) -> None:
        """Test that same location is a strong match signal."""
        issues = [
            Issue(
                IssueType.TEST_FAILURE,
                IssueSeverity.MAJOR,
                "Authentication failed",
                location="src/auth.py:42",
            ),
            Issue(
                IssueType.TEST_FAILURE,
                IssueSeverity.MAJOR,
                "Password validation error",
                location="src/auth.py:42",
            ),
            Issue(
                IssueType.TEST_FAILURE,
                IssueSeverity.MAJOR,
                "Database connection timeout",
                location="src/db.py:100",
            ),
        ]

        groups = history_manager.group_similar_issues(issues)

        # Issues at same location should be grouped together even with different titles
        auth_group = [g for g in groups if any(i.location == "src/auth.py:42" for i in g)]
        assert len(auth_group) == 1
        assert len(auth_group[0]) == 2

    def test_has_recurring_issues_true_when_threshold_exceeded(
        self, history_manager, sample_task
    ) -> None:
        """Test has_recurring_issues returns True when threshold exceeded."""
        # Record 3 iterations with the same issue
        same_issue = Issue(IssueType.TEST_FAILURE, IssueSeverity.MAJOR, "Same test fails")

        for i in range(1, 4):
            history_manager.record_iteration(
                task_id=sample_task["id"],
                iteration=i,
                status="invalid",
                issues=[same_issue],
            )

        result = history_manager.has_recurring_issues(sample_task["id"], threshold=2)
        assert result is True

    def test_has_recurring_issues_false_below_threshold(self, history_manager, sample_task) -> None:
        """Test has_recurring_issues returns False below threshold."""
        # Record 2 iterations with the same issue
        same_issue = Issue(IssueType.TEST_FAILURE, IssueSeverity.MAJOR, "Test fails")

        for i in range(1, 3):
            history_manager.record_iteration(
                task_id=sample_task["id"],
                iteration=i,
                status="invalid",
                issues=[same_issue],
            )

        result = history_manager.has_recurring_issues(sample_task["id"], threshold=3)
        assert result is False

    def test_has_recurring_issues_false_for_different_issues(
        self, history_manager, sample_task
    ) -> None:
        """Test has_recurring_issues returns False for different issues each time."""
        # Record iterations with completely different issues
        different_issues = [
            Issue(IssueType.TEST_FAILURE, IssueSeverity.MAJOR, "Authentication test failed"),
            Issue(IssueType.LINT_ERROR, IssueSeverity.MINOR, "Unused import detected"),
            Issue(IssueType.TYPE_ERROR, IssueSeverity.BLOCKER, "Database connection timeout"),
        ]
        for i, issue in enumerate(different_issues, 1):
            history_manager.record_iteration(
                task_id=sample_task["id"],
                iteration=i,
                status="invalid",
                issues=[issue],
            )

        result = history_manager.has_recurring_issues(sample_task["id"], threshold=2)
        assert result is False

    def test_get_recurring_issue_summary_returns_grouped_analysis(
        self, history_manager, sample_task
    ) -> None:
        """Test get_recurring_issue_summary returns grouped analysis."""
        # Record multiple iterations with recurring issues
        auth_issue = Issue(IssueType.TEST_FAILURE, IssueSeverity.BLOCKER, "Auth test failed")
        lint_issue = Issue(IssueType.LINT_ERROR, IssueSeverity.MINOR, "Unused import")

        history_manager.record_iteration(
            task_id=sample_task["id"],
            iteration=1,
            status="invalid",
            issues=[auth_issue, lint_issue],
        )
        history_manager.record_iteration(
            task_id=sample_task["id"],
            iteration=2,
            status="invalid",
            issues=[auth_issue],  # Auth issue recurs
        )
        history_manager.record_iteration(
            task_id=sample_task["id"],
            iteration=3,
            status="invalid",
            issues=[auth_issue],  # Auth issue recurs again
        )

        summary = history_manager.get_recurring_issue_summary(sample_task["id"])

        assert summary is not None
        assert "recurring_issues" in summary
        assert len(summary["recurring_issues"]) >= 1
        # Auth issue should be identified as recurring
        recurring_titles = [r["title"] for r in summary["recurring_issues"]]
        assert any("auth" in t.lower() for t in recurring_titles)

    def test_get_recurring_issue_summary_includes_count(self, history_manager, sample_task) -> None:
        """Test that recurring issue summary includes occurrence count."""
        issue = Issue(IssueType.TEST_FAILURE, IssueSeverity.MAJOR, "Recurring error")

        for i in range(1, 5):
            history_manager.record_iteration(
                task_id=sample_task["id"], iteration=i, status="invalid", issues=[issue]
            )

        summary = history_manager.get_recurring_issue_summary(sample_task["id"])

        assert summary["recurring_issues"][0]["count"] == 4

    def test_get_recurring_issue_summary_empty_history(self, history_manager, sample_task) -> None:
        """Test get_recurring_issue_summary with no history."""
        summary = history_manager.get_recurring_issue_summary(sample_task["id"])

        assert summary["recurring_issues"] == []
        assert summary["total_iterations"] == 0

    def test_group_similar_issues_empty_list(self, history_manager) -> None:
        """Test group_similar_issues with empty list."""
        groups = history_manager.group_similar_issues([])
        assert groups == []

    def test_group_similar_issues_single_issue(self, history_manager) -> None:
        """Test group_similar_issues with single issue."""
        issues = [Issue(IssueType.TEST_FAILURE, IssueSeverity.MAJOR, "Single issue")]
        groups = history_manager.group_similar_issues(issues)
        assert len(groups) == 1
        assert len(groups[0]) == 1
