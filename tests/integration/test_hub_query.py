"""Integration tests for hub query MCP tools.

These tests verify the hub query tools work correctly against real databases
with data from multiple projects.
"""

import tempfile
import uuid

import pytest

from gobby.mcp_proxy.tools.hub import create_hub_registry
from gobby.storage.tasks import LocalTaskManager

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


def _test_uuid(label: str) -> str:
    """Deterministic UUID for fixture rows — projects/sessions/tasks PKs are UUID-typed."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gobby-hub-query-tests/{label}"))


PROJECT_FRONTEND = _test_uuid("project-frontend")
PROJECT_BACKEND = _test_uuid("project-backend")


def _start_current_stage(task_manager: LocalTaskManager, task_id: str) -> None:
    current = task_manager.stage_states.current_stage(task_id)
    if current is None:
        task_manager.initialize_task_manifest(task_id)
        current = task_manager.stage_states.current_stage(task_id)
    assert current is not None
    task_manager.stage_states.start_stage(task_id, current.stage_name, by_session_id=None)


@pytest.fixture
def multi_project_hub(hub_db):
    """Create a hub database with data from multiple projects."""
    # Insert data for two projects
    for i, (project_name, project_id) in enumerate(
        [("project-frontend", PROJECT_FRONTEND), ("project-backend", PROJECT_BACKEND)]
    ):
        project_dir = tempfile.mkdtemp()

        # Insert project
        hub_db.execute(
            """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (project_id, project_name.replace("-", " ").title()),
        )

        task_manager = LocalTaskManager(hub_db)
        # Insert tasks for this project
        for j, (state, task_type) in enumerate(
            [
                ("ready", "task"),
                ("in_progress", "feature"),
                ("closed", "bug"),
            ]
        ):
            task = task_manager.create_task(
                project_id=project_id,
                title=f"Task {j} for {project_name}",
                task_type=task_type,
                priority=j + 1,
                validation_criteria="Test task completion is observable.",
            )
            if state == "in_progress":
                _start_current_stage(task_manager, task.id)
            elif state == "closed":
                task_manager.close_task(task.id)

        # Insert sessions for this project
        for k, (source, status) in enumerate(
            [
                ("claude", "active"),
                ("qwen", "ended"),
            ]
        ):
            hub_db.execute(
                """
                INSERT INTO sessions (id, project_id, external_id, source, machine_id, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    _test_uuid(f"sess-{project_name}-{k}"),
                    project_id,
                    f"ext-{project_name}-{k}",
                    source,
                    f"21000000-0000-4000-8000-{i + 1:012d}",
                    status,
                ),
            )

    return hub_db


class TestHubQueryIntegration:
    """Integration tests for hub query tools with multi-project data."""

    def test_list_all_projects_returns_all_projects(self, multi_project_hub) -> None:
        """Test that list_all_projects returns all projects from hub."""
        import asyncio

        registry = create_hub_registry(db=multi_project_hub)
        tool = registry.get_tool("list_all_projects")
        assert tool is not None

        result = asyncio.run(tool())

        assert result["success"] is True
        assert result["project_count"] == 2

        project_ids = [p["project_id"] for p in result["projects"]]
        assert PROJECT_FRONTEND in project_ids
        assert PROJECT_BACKEND in project_ids

    def test_list_all_projects_includes_accurate_counts(self, multi_project_hub) -> None:
        """Test that list_all_projects includes correct task and session counts."""
        import asyncio

        registry = create_hub_registry(db=multi_project_hub)
        tool = registry.get_tool("list_all_projects")
        assert tool is not None

        result = asyncio.run(tool())

        assert result["success"] is True

        for project in result["projects"]:
            # Each project has 3 tasks and 2 sessions
            assert project["task_count"] == 3
            assert project["session_count"] == 2

    def test_list_cross_project_tasks_returns_tasks_from_all_projects(
        self, multi_project_hub
    ) -> None:
        """Test that list_cross_project_tasks returns tasks from multiple projects."""
        import asyncio

        registry = create_hub_registry(db=multi_project_hub)
        tool = registry.get_tool("list_cross_project_tasks")
        assert tool is not None

        result = asyncio.run(tool())

        assert result["success"] is True
        assert result["count"] == 6  # 3 tasks per project * 2 projects

        # Verify tasks from both projects are present
        project_ids = {t["project_id"] for t in result["tasks"]}
        assert PROJECT_FRONTEND in project_ids
        assert PROJECT_BACKEND in project_ids

    def test_list_cross_project_tasks_filters_by_state(self, multi_project_hub) -> None:
        """Test that list_cross_project_tasks correctly filters by projected state."""
        import asyncio

        registry = create_hub_registry(db=multi_project_hub)
        tool = registry.get_tool("list_cross_project_tasks")
        assert tool is not None

        # Filter for ready tasks only
        result = asyncio.run(tool(state="ready"))

        assert result["success"] is True
        assert result["count"] == 2  # 1 ready task per project

        for task in result["tasks"]:
            assert task["state"]["current_stage"] is None
            assert task["state"]["is_closed"] is False

    def test_list_cross_project_tasks_respects_limit(self, multi_project_hub) -> None:
        """Test that list_cross_project_tasks respects the limit parameter."""
        import asyncio

        registry = create_hub_registry(db=multi_project_hub)
        tool = registry.get_tool("list_cross_project_tasks")
        assert tool is not None

        result = asyncio.run(tool(limit=3))

        assert result["success"] is True
        assert result["count"] == 3

    def test_list_cross_project_sessions_returns_sessions_from_all_projects(
        self, multi_project_hub
    ) -> None:
        """Test that list_cross_project_sessions returns sessions from multiple projects."""
        import asyncio

        registry = create_hub_registry(db=multi_project_hub)
        tool = registry.get_tool("list_cross_project_sessions")
        assert tool is not None

        result = asyncio.run(tool())

        assert result["success"] is True
        assert result["count"] == 4  # 2 sessions per project * 2 projects

        # Verify sessions from both projects are present
        project_ids = {s["project_id"] for s in result["sessions"]}
        assert PROJECT_FRONTEND in project_ids
        assert PROJECT_BACKEND in project_ids

    def test_list_cross_project_sessions_respects_limit(self, multi_project_hub) -> None:
        """Test that list_cross_project_sessions respects the limit parameter."""
        import asyncio

        registry = create_hub_registry(db=multi_project_hub)
        tool = registry.get_tool("list_cross_project_sessions")
        assert tool is not None

        result = asyncio.run(tool(limit=2))

        assert result["success"] is True
        assert result["count"] == 2

    def test_hub_stats_returns_accurate_aggregates(self, multi_project_hub) -> None:
        """Test that hub_stats returns accurate aggregate statistics."""
        import asyncio

        registry = create_hub_registry(db=multi_project_hub)
        tool = registry.get_tool("hub_stats")
        assert tool is not None

        result = asyncio.run(tool())

        assert result["success"] is True
        stats = result["stats"]

        # 2 projects
        assert stats["project_count"] == 2

        # 6 total tasks (3 per project)
        assert stats["tasks"]["total"] == 6
        # State breakdown: 2 ready, 2 in_progress, 2 closed
        assert stats["tasks"]["by_state"]["ready"] == 2
        assert stats["tasks"]["by_state"]["in_progress"] == 2
        assert stats["tasks"]["by_state"]["closed"] == 2

        # 4 total sessions (2 per project)
        assert stats["sessions"]["total"] == 4
        # Status breakdown: 2 active, 2 ended
        assert stats["sessions"]["by_status"]["active"] == 2
        assert stats["sessions"]["by_status"]["ended"] == 2


class TestHubQueryEdgeCases:
    """Integration tests for hub query edge cases."""

    def test_hub_tools_handle_missing_database(self) -> None:
        """Test that all hub tools handle missing database gracefully."""
        import asyncio

        registry = create_hub_registry(db=None)

        # Test all tools handle missing db
        for tool_name in [
            "list_all_projects",
            "list_cross_project_tasks",
            "list_cross_project_sessions",
            "hub_stats",
        ]:
            tool = registry.get_tool(tool_name)
            assert tool is not None
            result = asyncio.run(tool())
            assert result["success"] is False
            assert "not available" in result["error"]

    def test_hub_tools_handle_empty_database(self, hub_db) -> None:
        """Test that all hub tools handle empty database gracefully."""
        import asyncio

        registry = create_hub_registry(db=hub_db)

        # list_all_projects should return empty list
        tool = registry.get_tool("list_all_projects")
        assert tool is not None
        result = asyncio.run(tool())
        assert result["success"] is True
        assert result["project_count"] == 0

        # list_cross_project_tasks should return empty list
        tool = registry.get_tool("list_cross_project_tasks")
        assert tool is not None
        result = asyncio.run(tool())
        assert result["success"] is True
        assert result["count"] == 0

        # list_cross_project_sessions should return empty list
        tool = registry.get_tool("list_cross_project_sessions")
        assert tool is not None
        result = asyncio.run(tool())
        assert result["success"] is True
        assert result["count"] == 0

        # hub_stats should return zeros
        tool = registry.get_tool("hub_stats")
        assert tool is not None
        result = asyncio.run(tool())
        assert result["success"] is True
        assert result["stats"]["project_count"] == 0
        assert result["stats"]["tasks"]["total"] == 0
        assert result["stats"]["sessions"]["total"] == 0

    def test_projects_with_only_tasks_no_sessions(self, hub_db) -> None:
        """Test that list_all_projects handles projects with only tasks."""
        import asyncio

        # Insert project with only tasks, no sessions
        tasks_only_project = _test_uuid("tasks-only-project")
        hub_db.execute(
            """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (tasks_only_project, "Tasks Only"),
        )
        hub_db.execute(
            """
            INSERT INTO tasks (
                id, project_id, title, validation_criteria, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                _test_uuid("task-only-1"),
                tasks_only_project,
                "A Task",
                "The task is visible in project queries.",
            ),
        )

        registry = create_hub_registry(db=hub_db)
        tool = registry.get_tool("list_all_projects")
        assert tool is not None
        result = asyncio.run(tool())

        assert result["success"] is True
        assert result["project_count"] == 1
        project = result["projects"][0]
        assert project["project_id"] == tasks_only_project
        assert project["task_count"] == 1
        assert project["session_count"] == 0

    def test_projects_with_only_sessions_no_tasks(self, hub_db) -> None:
        """Test that list_all_projects handles projects with only sessions."""
        import asyncio

        # Insert project with only sessions, no tasks
        sessions_only_project = _test_uuid("sessions-only-project")
        hub_db.execute(
            """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (sessions_only_project, "Sessions Only"),
        )
        hub_db.execute(
            """
            INSERT INTO sessions (id, project_id, external_id, source, machine_id, status, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                _test_uuid("sess-only-1"),
                sessions_only_project,
                "ext-1",
                "claude",
                "21000000-0000-4000-8000-000000000001",
                "active",
            ),
        )

        registry = create_hub_registry(db=hub_db)
        tool = registry.get_tool("list_all_projects")
        assert tool is not None
        result = asyncio.run(tool())

        assert result["success"] is True
        assert result["project_count"] == 1
        project = result["projects"][0]
        assert project["project_id"] == sessions_only_project
        assert project["task_count"] == 0
        assert project["session_count"] == 1
