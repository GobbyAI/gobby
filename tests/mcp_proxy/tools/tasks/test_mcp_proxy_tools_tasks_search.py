"""Tests for task search MCP tools.

Exercises the real create_search_registry function and its registered
tools (search_tasks, reindex_tasks) with all code paths:
- Empty/whitespace query
- Successful search with real task data
- Comma-separated current stage-state filter
- Project filter error
- Parent task ID resolution (success and failure)
- Reindex success and error
- Various filter combinations
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks._search import create_reindex_registry, create_search_registry
from gobby.storage.tasks import TaskNotFoundError

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit

COMPACT_STATE_KEYS = {
    "current_stage",
    "is_closed",
    "closed_at",
    "is_claimed",
    "is_blocked",
    "is_escalated",
}

SEARCH_TASK_KEYS = {
    "ref",
    "id",
    "seq_num",
    "title",
    "task_type",
    "category",
    "priority",
    "path_cache",
    "updated_at",
    "state",
    "score",
    "match_preview",
}

NOISY_TASK_KEYS = {
    "description",
    "validation_criteria",
    "claimed_by_session_id",
    "closed_in_session_id",
    "closed_commit_sha",
    "validation_status",
    "validation_feedback",
    "validation_fail_count",
    "dispatch_failure_count",
    "validation_override_reason",
    "merge_in_progress",
    "blocked_by_merge",
    "commits",
    "github_issue_number",
    "github_pr_number",
    "github_repo",
    "linear_issue_id",
    "linear_team_id",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def task_manager(temp_db: HubDatabase) -> LocalTaskManager:
    """Create a real LocalTaskManager backed by temp database."""
    from gobby.storage.tasks import LocalTaskManager

    return LocalTaskManager(temp_db)


@pytest.fixture
def real_project(project_manager: LocalProjectManager) -> dict:
    """Create a real project for task scoping."""
    project = project_manager.create(
        name="search-test-project",
        repo_path="/tmp/search-test",
    )
    return project.to_dict()


@pytest.fixture
def seeded_tasks(
    task_manager: LocalTaskManager,
    real_project: dict,
) -> list:
    """Create several tasks for search tests."""
    tasks = []
    task_data = [
        (
            "Fix login authentication bug",
            "bug",
            1,
            "Authentication sessions fail when refresh tokens expire.",
        ),
        (
            "Add user profile page",
            "feature",
            2,
            "Profile page should display user settings and account metadata.",
        ),
        (
            "Refactor database connection pooling",
            "task",
            2,
            "Database connection pooling needs clearer ownership and lifecycle boundaries.",
        ),
        (
            "Write unit tests for auth module",
            "task",
            3,
            "Auth module tests should cover token refresh and login failure branches.",
        ),
        (
            "Deploy to staging environment",
            "task",
            2,
            "Staging deployment should publish the current build and verify health checks.",
        ),
    ]
    for title, task_type, priority, description in task_data:
        task = task_manager.create_task(
            project_id=real_project["id"],
            title=title,
            description=description,
            task_type=task_type,
            priority=priority,
            validation_criteria="Test task completion is observable.",
        )
        tasks.append(task)
    return tasks


def _make_ctx(
    task_manager: LocalTaskManager,
    project_id: str | None = None,
) -> MagicMock:
    """Create a RegistryContext-like mock that delegates to real task_manager."""
    ctx = MagicMock()
    ctx.task_manager = task_manager
    ctx.resolve_project_filter.return_value = project_id
    return ctx


# ---------------------------------------------------------------------------
# search_tasks tool
# ---------------------------------------------------------------------------


class TestSearchTasksValidation:
    """Tests for input validation in search_tasks."""

    def test_empty_query_returns_error(self, task_manager: LocalTaskManager) -> None:
        ctx = _make_ctx(task_manager)
        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        result = func(query="")
        assert result["error"] == "Query is required"
        assert result["tasks"] == []
        assert result["count"] == 0

    @pytest.mark.parametrize("query", ["   ", "\t\n"])
    def test_whitespace_only_query_returns_error(
        self, task_manager: LocalTaskManager, query: str
    ) -> None:
        ctx = _make_ctx(task_manager)
        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        result = func(query=query)
        assert "error" in result
        assert result["count"] == 0


class TestSearchTasksProjectFilter:
    """Tests for project filter handling."""

    def test_project_filter_error_returns_error(self, task_manager: LocalTaskManager) -> None:
        ctx = _make_ctx(task_manager)
        ctx.resolve_project_filter.side_effect = ValueError("Project not found: nonexistent")

        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        result = func(query="test")
        assert "error" in result
        assert "Project not found" in result["error"]
        assert result["tasks"] == []
        assert result["count"] == 0

    def test_project_filter_called_with_params(self, task_manager: LocalTaskManager) -> None:
        ctx = _make_ctx(task_manager)

        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        func(query="test", project="my-project", all_projects=True)
        ctx.resolve_project_filter.assert_called_once_with("my-project", True)
        assert ctx.resolve_project_filter.call_count == 1
        assert ctx.resolve_project_filter.call_args is not None


class TestSearchTasksResults:
    """Tests for actual search results using real task data."""

    def test_successful_search_returns_results(
        self,
        task_manager: LocalTaskManager,
        real_project: dict,
        seeded_tasks: list,
    ) -> None:
        ctx = _make_ctx(task_manager, project_id=real_project["id"])
        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        result = func(query="authentication")

        assert result["count"] > 0
        assert "query" in result
        assert result["query"] == "authentication"

    def test_search_results_have_score(
        self,
        task_manager: LocalTaskManager,
        real_project: dict,
        seeded_tasks: list,
    ) -> None:
        ctx = _make_ctx(task_manager, project_id=real_project["id"])
        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        result = func(query="login bug")

        assert result["count"] > 0
        first_task = result["tasks"][0]
        assert "score" in first_task
        assert isinstance(first_task["score"], float)
        assert "match_preview" in first_task
        assert len(first_task["match_preview"]) <= 160

    def test_search_results_include_compact_fields(
        self,
        task_manager: LocalTaskManager,
        real_project: dict,
        seeded_tasks: list,
    ) -> None:
        ctx = _make_ctx(task_manager, project_id=real_project["id"])
        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        result = func(query="database")

        assert result["count"] > 0
        for task in result["tasks"]:
            assert set(task) == SEARCH_TASK_KEYS
            assert set(task["state"]) == COMPACT_STATE_KEYS
            assert isinstance(task["updated_at"], str | type(None))
            assert NOISY_TASK_KEYS.isdisjoint(task)

    def test_search_match_preview_is_bounded_and_normalized(
        self,
        task_manager: LocalTaskManager,
        real_project: dict,
        seeded_tasks: list,
    ) -> None:
        ctx = _make_ctx(task_manager, project_id=real_project["id"])
        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")
        long_description = "First line\n\n" + "token  " * 80
        task_manager.create_task(
            project_id=real_project["id"],
            title="Bounded preview marker",
            description=long_description,
            task_type="task",
            priority=2,
            validation_criteria="Test task completion is observable.",
        )

        result = func(query="Bounded preview marker")

        assert result["count"] > 0
        match = next(task for task in result["tasks"] if task["title"] == "Bounded preview marker")
        preview = match["match_preview"]
        assert len(preview) <= 160
        assert "\n" not in preview
        assert "  " not in preview
        assert preview.endswith("...")

    def test_query_is_stripped(
        self,
        task_manager: LocalTaskManager,
        real_project: dict,
        seeded_tasks: list,
    ) -> None:
        ctx = _make_ctx(task_manager, project_id=real_project["id"])
        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        result = func(query="  authentication  ")

        assert result["query"] == "authentication"

    def test_no_matching_results_returns_empty(
        self,
        task_manager: LocalTaskManager,
        real_project: dict,
        seeded_tasks: list,
    ) -> None:
        ctx = _make_ctx(task_manager, project_id=real_project["id"])
        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        result = func(query="xyzzynonexistent12345")

        assert result["count"] == 0
        assert result["tasks"] == []

    def test_query_syntax_error_returns_actionable_typed_failure(
        self,
        task_manager: LocalTaskManager,
    ) -> None:
        from gobby.search.keyword import SearchQuerySyntaxError

        ctx = _make_ctx(task_manager)
        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        with patch.object(
            task_manager,
            "search_tasks",
            side_effect=SearchQuerySyntaxError("title:("),
        ):
            result = func(query=" title:( ")

        assert result == {
            "error": (
                "Search query could not be parsed: 'title:('. "
                "Simplify the query to plain words and retry."
            ),
            "error_code": "SEARCH_QUERY_SYNTAX_ERROR",
            "tasks": [],
            "count": 0,
            "query": "title:(",
        }

    def test_search_infrastructure_error_propagates(
        self,
        task_manager: LocalTaskManager,
    ) -> None:
        ctx = _make_ctx(task_manager)
        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        with (
            patch.object(
                task_manager,
                "search_tasks",
                side_effect=RuntimeError("database connection lost"),
            ),
            pytest.raises(RuntimeError, match="connection lost"),
        ):
            func(query="alpha")


class TestSearchTasksStageStateFilter:
    """Tests for current-stage-state filter handling."""

    def test_single_stage_state_passed_as_string(
        self,
        task_manager: LocalTaskManager,
        real_project: dict,
    ) -> None:
        ctx = _make_ctx(task_manager, project_id=real_project["id"])
        # Use a mock to verify the stage state is passed as-is.
        ctx.task_manager = MagicMock()
        ctx.task_manager.search_tasks.return_value = []

        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        func(query="test", current_stage_state="ready")

        call_kwargs = ctx.task_manager.search_tasks.call_args[1]
        assert call_kwargs["current_stage_state"] == "ready"

    def test_list_stage_state_passed_through(
        self,
        task_manager: LocalTaskManager,
    ) -> None:
        ctx = _make_ctx(task_manager)
        ctx.task_manager = MagicMock()
        ctx.task_manager.search_tasks.return_value = []

        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        func(query="test", current_stage_state=["ready", "needs_review"])

        call_kwargs = ctx.task_manager.search_tasks.call_args[1]
        assert call_kwargs["current_stage_state"] == ["ready", "needs_review"]

    def test_comma_stage_state_splits_correctly(
        self,
        task_manager: LocalTaskManager,
    ) -> None:
        ctx = _make_ctx(task_manager)
        ctx.task_manager = MagicMock()
        ctx.task_manager.search_tasks.return_value = []

        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        func(query="test", current_stage_state="ready, in_progress, needs_review")

        call_kwargs = ctx.task_manager.search_tasks.call_args[1]
        assert call_kwargs["current_stage_state"] == [
            "ready",
            "in_progress",
            "needs_review",
        ]


class TestSearchTasksParentFilter:
    """Tests for parent_task_id resolution."""

    @patch("gobby.mcp_proxy.tools.tasks._search.resolve_task_id_for_mcp")
    def test_parent_task_id_resolved(
        self,
        mock_resolve: MagicMock,
        task_manager: LocalTaskManager,
    ) -> None:
        mock_resolve.return_value = "parent-uuid-123"
        ctx = _make_ctx(task_manager)
        ctx.task_manager = MagicMock()
        ctx.task_manager.search_tasks.return_value = []

        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        func(query="test", parent_task_id="#10")

        call_kwargs = ctx.task_manager.search_tasks.call_args[1]
        assert call_kwargs["parent_task_id"] == "parent-uuid-123"
        mock_resolve.assert_called_once()

    @patch("gobby.mcp_proxy.tools.tasks._search.resolve_task_id_for_mcp")
    def test_invalid_parent_task_id_returns_error(
        self,
        mock_resolve: MagicMock,
        task_manager: LocalTaskManager,
    ) -> None:
        mock_resolve.side_effect = TaskNotFoundError("bad-ref")
        ctx = _make_ctx(task_manager)

        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        result = func(query="test", parent_task_id="bad-ref")

        assert "error" in result
        assert "Invalid parent_task_id" in result["error"]
        assert result["tasks"] == []
        assert result["count"] == 0

    def test_no_parent_task_id_passes_none(
        self,
        task_manager: LocalTaskManager,
    ) -> None:
        ctx = _make_ctx(task_manager)
        ctx.task_manager = MagicMock()
        ctx.task_manager.search_tasks.return_value = []

        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        func(query="test")

        call_kwargs = ctx.task_manager.search_tasks.call_args[1]
        assert call_kwargs["parent_task_id"] is None


class TestSearchTasksAllFilters:
    """Tests for passing all filter parameters through."""

    def test_all_filters_forwarded_to_task_manager(
        self,
        task_manager: LocalTaskManager,
    ) -> None:
        ctx = _make_ctx(task_manager)
        ctx.task_manager = MagicMock()
        ctx.task_manager.search_tasks.return_value = []

        registry = create_search_registry(ctx)
        func = registry.get_tool("search_tasks")

        func(
            query="  test query  ",
            current_stage_state="ready",
            task_type="bug",
            priority=1,
            category="code",
            limit=5,
            min_score=0.5,
        )

        call_kwargs = ctx.task_manager.search_tasks.call_args[1]
        assert call_kwargs["query"] == "test query"
        assert call_kwargs["current_stage_state"] == "ready"
        assert call_kwargs["task_type"] == "bug"
        assert call_kwargs["priority"] == 1
        assert call_kwargs["category"] == "code"
        assert call_kwargs["limit"] == 5
        assert call_kwargs["min_score"] == 0.5


# ---------------------------------------------------------------------------
# reindex_tasks tool
# ---------------------------------------------------------------------------


class TestReindexTasks:
    """Tests for the reindex_tasks tool (now in create_reindex_registry)."""

    def test_successful_reindex(self, task_manager: LocalTaskManager) -> None:
        ctx = _make_ctx(task_manager)
        ctx.task_manager = MagicMock()
        ctx.task_manager.reindex_search.return_value = {"item_count": 50}

        registry = create_reindex_registry(ctx)
        func = registry.get_tool("reindex_tasks")

        result = func()

        assert "50 tasks" in result["message"]
        assert result["stats"]["item_count"] == 50

    def test_reindex_with_project_filter(self, task_manager: LocalTaskManager) -> None:
        ctx = _make_ctx(task_manager, project_id="proj-abc")
        ctx.task_manager = MagicMock()
        ctx.task_manager.reindex_search.return_value = {"item_count": 10}

        registry = create_reindex_registry(ctx)
        func = registry.get_tool("reindex_tasks")

        func(project="my-project")

        ctx.resolve_project_filter.assert_called_once_with("my-project", False)
        assert ctx.resolve_project_filter.call_count == 1
        assert ctx.resolve_project_filter.call_args is not None

    def test_reindex_all_projects(self, task_manager: LocalTaskManager) -> None:
        ctx = _make_ctx(task_manager)
        ctx.task_manager = MagicMock()
        ctx.task_manager.reindex_search.return_value = {"item_count": 100}

        registry = create_reindex_registry(ctx)
        func = registry.get_tool("reindex_tasks")

        func(all_projects=True)

        ctx.resolve_project_filter.assert_called_once_with(None, True)
        assert ctx.resolve_project_filter.call_count == 1
        assert ctx.resolve_project_filter.call_args is not None

    def test_reindex_project_filter_error(self, task_manager: LocalTaskManager) -> None:
        ctx = _make_ctx(task_manager)
        ctx.resolve_project_filter.side_effect = ValueError("Project not found: bad")

        registry = create_reindex_registry(ctx)
        func = registry.get_tool("reindex_tasks")

        result = func(project="bad")

        assert "error" in result
        assert "Project not found" in result["error"]

    def test_reindex_returns_zero_for_empty_project(
        self,
        task_manager: LocalTaskManager,
    ) -> None:
        ctx = _make_ctx(task_manager)
        ctx.task_manager = MagicMock()
        ctx.task_manager.reindex_search.return_value = {}

        registry = create_reindex_registry(ctx)
        func = registry.get_tool("reindex_tasks")

        result = func()

        # item_count missing from stats -> .get('item_count', 0) = 0
        assert "0 tasks" in result["message"]


# ---------------------------------------------------------------------------
# Registry structure tests
# ---------------------------------------------------------------------------


class TestRegistryStructure:
    """Tests for the registry created by create_search_registry."""

    def test_registry_has_search_tasks_tool(self, task_manager: LocalTaskManager) -> None:
        ctx = _make_ctx(task_manager)
        registry = create_search_registry(ctx)
        assert registry.get_tool("search_tasks") is not None

    def test_reindex_registry_has_reindex_tasks_tool(self, task_manager: LocalTaskManager) -> None:
        ctx = _make_ctx(task_manager)
        registry = create_reindex_registry(ctx)
        assert registry.get_tool("reindex_tasks") is not None

    def test_search_registry_has_one_tool(self, task_manager: LocalTaskManager) -> None:
        ctx = _make_ctx(task_manager)
        registry = create_search_registry(ctx)
        assert len(registry) == 1

    def test_registry_name(self, task_manager: LocalTaskManager) -> None:
        ctx = _make_ctx(task_manager)
        registry = create_search_registry(ctx)
        assert registry.name == "gobby-tasks-search"

    def test_search_tasks_schema_has_required_query(
        self,
        task_manager: LocalTaskManager,
    ) -> None:
        ctx = _make_ctx(task_manager)
        registry = create_search_registry(ctx)
        schema = registry.get_schema("search_tasks")
        assert schema is not None
        assert "query" in schema["inputSchema"]["required"]
