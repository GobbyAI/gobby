"""Integration tests for hub query MCP tools.

These tests verify the hub query tools work correctly against real databases
with data from multiple projects.
"""

import tempfile
from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.hub import create_hub_registry
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.tasks import LocalTaskManager

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


def _start_current_stage(task_manager: LocalTaskManager, task_id: str) -> None:
    current = task_manager.stage_states.current_stage(task_id)
    assert current is not None
    task_manager.stage_states.start_stage(task_id, current.stage_name, by_session_id=None)


@pytest.fixture
def hub_dir():
    """Create a temporary hub directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def multi_project_hub(hub_dir):
    """Create a hub database with data from multiple projects."""
    hub_db_path = hub_dir / "gobby-hub.db"
    hub_db = LocalDatabase(hub_db_path)
    run_migrations(hub_db)

    # Insert data for two projects
    for i, project_name in enumerate(["project-frontend", "project-backend"]):
        project_dir = tempfile.mkdtemp()

        # Insert project
        hub_db.execute(
            """
            INSERT INTO projects (id, name, repo_path, created_at, updated_at)
            VALUES (?, ?, ?, datetime('now'), datetime('now'))
            """,
            (project_name, project_name.replace("-", " ").title(), project_dir),
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
                project_id=project_name,
                title=f"Task {j} for {project_name}",
                task_type=task_type,
                priority=j + 1,
            )
            if state == "in_progress":
                _start_current_stage(task_manager, task.id)
            elif state == "closed":
                task_manager.close_task(task.id)

        # Insert sessions for this project
        for k, (source, status) in enumerate(
            [
                ("claude", "active"),
                ("gemini", "ended"),
            ]
        ):
            hub_db.execute(
                """
                INSERT INTO sessions (id, project_id, external_id, source, machine_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                """,
                (
                    f"sess-{project_name}-{k}",
                    project_name,
                    f"ext-{project_name}-{k}",
                    source,
                    f"machine-{i}",
                    status,
                ),
            )

    hub_db.close()
    return hub_db_path


class TestHubQueryIntegration:
    """Integration tests for hub query tools with multi-project data."""

    def test_list_all_projects_returns_all_projects(self, multi_project_hub) -> None:
        """Test that list_all_projects returns all projects from hub."""
        import asyncio

        registry = create_hub_registry(hub_db_path=multi_project_hub)
        tool = registry.get_tool("list_all_projects")
        assert tool is not None

        result = asyncio.run(tool())

        assert result["success"] is True
        assert result["project_count"] == 2

        project_ids = [p["project_id"] for p in result["projects"]]
        assert "project-frontend" in project_ids
        assert "project-backend" in project_ids

    def test_list_all_projects_includes_accurate_counts(self, multi_project_hub) -> None:
        """Test that list_all_projects includes correct task and session counts."""
        import asyncio

        registry = create_hub_registry(hub_db_path=multi_project_hub)
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

        registry = create_hub_registry(hub_db_path=multi_project_hub)
        tool = registry.get_tool("list_cross_project_tasks")
        assert tool is not None

        result = asyncio.run(tool())

        assert result["success"] is True
        assert result["count"] == 6  # 3 tasks per project * 2 projects

        # Verify tasks from both projects are present
        project_ids = {t["project_id"] for t in result["tasks"]}
        assert "project-frontend" in project_ids
        assert "project-backend" in project_ids

    def test_list_cross_project_tasks_filters_by_state(self, multi_project_hub) -> None:
        """Test that list_cross_project_tasks correctly filters by projected state."""
        import asyncio

        registry = create_hub_registry(hub_db_path=multi_project_hub)
        tool = registry.get_tool("list_cross_project_tasks")
        assert tool is not None

        # Filter for ready tasks only
        result = asyncio.run(tool(state="ready"))

        assert result["success"] is True
        assert result["count"] == 2  # 1 ready task per project

        for task in result["tasks"]:
            assert task["state"]["current_stage"]["state"] == "ready"

    def test_list_cross_project_tasks_respects_limit(self, multi_project_hub) -> None:
        """Test that list_cross_project_tasks respects the limit parameter."""
        import asyncio

        registry = create_hub_registry(hub_db_path=multi_project_hub)
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

        registry = create_hub_registry(hub_db_path=multi_project_hub)
        tool = registry.get_tool("list_cross_project_sessions")
        assert tool is not None

        result = asyncio.run(tool())

        assert result["success"] is True
        assert result["count"] == 4  # 2 sessions per project * 2 projects

        # Verify sessions from both projects are present
        project_ids = {s["project_id"] for s in result["sessions"]}
        assert "project-frontend" in project_ids
        assert "project-backend" in project_ids

    def test_list_cross_project_sessions_respects_limit(self, multi_project_hub) -> None:
        """Test that list_cross_project_sessions respects the limit parameter."""
        import asyncio

        registry = create_hub_registry(hub_db_path=multi_project_hub)
        tool = registry.get_tool("list_cross_project_sessions")
        assert tool is not None

        result = asyncio.run(tool(limit=2))

        assert result["success"] is True
        assert result["count"] == 2

    def test_hub_stats_returns_accurate_aggregates(self, multi_project_hub) -> None:
        """Test that hub_stats returns accurate aggregate statistics."""
        import asyncio

        registry = create_hub_registry(hub_db_path=multi_project_hub)
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

    def test_hub_tools_handle_missing_database(self, hub_dir) -> None:
        """Test that all hub tools handle missing database gracefully."""
        import asyncio

        nonexistent = hub_dir / "nonexistent.db"
        registry = create_hub_registry(hub_db_path=nonexistent)

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
            assert "not found" in result["error"]

    def test_hub_tools_handle_empty_database(self, hub_dir) -> None:
        """Test that all hub tools handle empty database gracefully."""
        import asyncio

        hub_db_path = hub_dir / "empty.db"
        db = LocalDatabase(hub_db_path)
        run_migrations(db)
        db.close()

        registry = create_hub_registry(hub_db_path=hub_db_path)

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

    def test_projects_with_only_tasks_no_sessions(self, hub_dir) -> None:
        """Test that list_all_projects handles projects with only tasks."""
        import asyncio

        hub_db_path = hub_dir / "partial.db"
        db = LocalDatabase(hub_db_path)
        run_migrations(db)

        # Insert project with only tasks, no sessions
        db.execute(
            """
            INSERT INTO projects (id, name, repo_path, created_at, updated_at)
            VALUES (?, ?, ?, datetime('now'), datetime('now'))
            """,
            ("tasks-only-project", "Tasks Only", "/path/tasks"),
        )
        db.execute(
            """
            INSERT INTO tasks (id, project_id, title, created_at, updated_at)
            VALUES (?, ?, ?, datetime('now'), datetime('now'))
            """,
            ("task-only-1", "tasks-only-project", "A Task"),
        )
        db.close()

        registry = create_hub_registry(hub_db_path=hub_db_path)
        tool = registry.get_tool("list_all_projects")
        assert tool is not None
        result = asyncio.run(tool())

        assert result["success"] is True
        assert result["project_count"] == 1
        project = result["projects"][0]
        assert project["project_id"] == "tasks-only-project"
        assert project["task_count"] == 1
        assert project["session_count"] == 0

    def test_projects_with_only_sessions_no_tasks(self, hub_dir) -> None:
        """Test that list_all_projects handles projects with only sessions."""
        import asyncio

        hub_db_path = hub_dir / "partial.db"
        db = LocalDatabase(hub_db_path)
        run_migrations(db)

        # Insert project with only sessions, no tasks
        db.execute(
            """
            INSERT INTO projects (id, name, repo_path, created_at, updated_at)
            VALUES (?, ?, ?, datetime('now'), datetime('now'))
            """,
            ("sessions-only-project", "Sessions Only", "/path/sessions"),
        )
        db.execute(
            """
            INSERT INTO sessions (id, project_id, external_id, source, machine_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            ("sess-only-1", "sessions-only-project", "ext-1", "claude", "machine-1", "active"),
        )
        db.close()

        registry = create_hub_registry(hub_db_path=hub_db_path)
        tool = registry.get_tool("list_all_projects")
        assert tool is not None
        result = asyncio.run(tool())

        assert result["success"] is True
        assert result["project_count"] == 1
        project = result["projects"][0]
        assert project["project_id"] == "sessions-only-project"
        assert project["task_count"] == 0
        assert project["session_count"] == 1
