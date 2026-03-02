"""
Tests for task_readiness.py MCP tools module.

This file tests the readiness tools that will be extracted from tasks.py
into task_readiness.py using Strangler Fig pattern.

RED PHASE: These tests will fail initially because task_readiness.py
does not exist yet. The module will be created in the green phase.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestListReadyTasks:
    """Tests for list_ready_tasks MCP tool."""

    def test_list_ready_tasks_basic(self, mock_readiness_registry) -> None:
        """Test basic list_ready_tasks call."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.to_brief.return_value = {
            "id": "task-1",
            "title": "Ready task",
            "status": "open",
        }
        task_manager.list_ready_tasks.return_value = [mock_task]

        registry = create_readiness_registry(task_manager=task_manager)
        list_ready = registry.get_tool("list_ready_tasks")

        result = list_ready()

        assert result["count"] == 1
        assert len(result["tasks"]) == 1
        task_manager.list_ready_tasks.assert_called_once()

    def test_list_ready_tasks_with_filters(self, mock_readiness_registry) -> None:
        """Test list_ready_tasks with priority and type filters."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        task_manager.list_ready_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)
        list_ready = registry.get_tool("list_ready_tasks")

        list_ready(priority=1, task_type="bug", assignee="dev1", limit=5)

        task_manager.list_ready_tasks.assert_called_once_with(
            priority=1,
            task_type="bug",
            assignee="dev1",
            parent_task_id=None,
            limit=5,
            project_id="test-project-id",
        )

    def test_list_ready_tasks_parent_filter(self, mock_readiness_registry) -> None:
        """Test filtering ready tasks by parent_task_id."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        task_manager.list_ready_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)
        list_ready = registry.get_tool("list_ready_tasks")

        list_ready(parent_task_id="parent-123")

        task_manager.list_ready_tasks.assert_called_once()
        call_args = task_manager.list_ready_tasks.call_args
        assert call_args.kwargs["parent_task_id"] == "parent-123"

    def test_list_ready_tasks_all_projects(self, mock_readiness_registry) -> None:
        """Test list_ready_tasks with all_projects=True."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        task_manager.list_ready_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)
        list_ready = registry.get_tool("list_ready_tasks")

        list_ready(all_projects=True)

        task_manager.list_ready_tasks.assert_called_once()
        call_args = task_manager.list_ready_tasks.call_args
        assert call_args.kwargs["project_id"] is None

    def test_list_ready_tasks_empty_result(self, mock_readiness_registry) -> None:
        """Test list_ready_tasks when no tasks are ready."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        task_manager.list_ready_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)
        list_ready = registry.get_tool("list_ready_tasks")

        result = list_ready()

        assert result["count"] == 0
        assert result["tasks"] == []


class TestListBlockedTasks:
    """Tests for list_blocked_tasks MCP tool."""

    def test_list_blocked_tasks_basic(self, mock_readiness_registry) -> None:
        """Test basic list_blocked_tasks call."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.to_brief.return_value = {
            "id": "task-1",
            "title": "Blocked task",
            "status": "open",
        }
        task_manager.list_blocked_tasks.return_value = [mock_task]

        registry = create_readiness_registry(task_manager=task_manager)
        list_blocked = registry.get_tool("list_blocked_tasks")

        result = list_blocked()

        assert result["count"] == 1
        assert len(result["tasks"]) == 1
        task_manager.list_blocked_tasks.assert_called_once()

    def test_list_blocked_tasks_parent_filter(self, mock_readiness_registry) -> None:
        """Test list_blocked_tasks with parent_task_id filter."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        task_manager.list_blocked_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)
        list_blocked = registry.get_tool("list_blocked_tasks")

        list_blocked(parent_task_id="parent-123", limit=10)

        task_manager.list_blocked_tasks.assert_called_once_with(
            parent_task_id="parent-123",
            limit=10,
            project_id="test-project-id",
        )

    def test_list_blocked_tasks_all_projects(self, mock_readiness_registry) -> None:
        """Test list_blocked_tasks with all_projects=True."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        task_manager.list_blocked_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)
        list_blocked = registry.get_tool("list_blocked_tasks")

        list_blocked(all_projects=True)

        task_manager.list_blocked_tasks.assert_called_once()
        call_args = task_manager.list_blocked_tasks.call_args
        assert call_args.kwargs["project_id"] is None

    def test_list_blocked_tasks_empty_result(self, mock_readiness_registry) -> None:
        """Test list_blocked_tasks when no tasks are blocked."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        task_manager.list_blocked_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)
        list_blocked = registry.get_tool("list_blocked_tasks")

        result = list_blocked()

        assert result["count"] == 0
        assert result["tasks"] == []


class TestSuggestNextTask:
    """Tests for suggest_next_task MCP tool."""

    def test_suggest_next_task_basic(self, mock_readiness_registry) -> None:
        """Test basic suggest_next_task call."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.priority = 1
        mock_task.complexity_score = 3
        mock_task.category = "Unit tests"
        mock_task.to_brief.return_value = {"id": "task-1", "title": "High priority task"}

        task_manager.list_ready_tasks.return_value = [mock_task]
        task_manager.list_tasks.return_value = []  # No children = leaf task

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        result = suggest()

        assert result["suggestion"] is not None
        assert result["suggestion"]["id"] == "task-1"
        assert "reason" in result

    def test_suggest_next_task_no_ready_tasks(self, mock_readiness_registry) -> None:
        """Test suggest_next_task when no tasks are ready."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        task_manager.list_ready_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        result = suggest()

        assert result["suggestion"] is None
        assert "No ready tasks" in result["reason"]

    def test_suggest_next_task_prefers_leaf_tasks(self, mock_readiness_registry) -> None:
        """Test that suggest_next_task prefers leaf tasks over parent tasks at same priority."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()

        # Parent task (has children) - same priority as leaf
        parent_task = MagicMock()
        parent_task.id = "parent-1"
        parent_task.priority = 1
        parent_task.complexity_score = None
        parent_task.category = None
        parent_task.to_brief.return_value = {"ref": "#1", "id": "parent-1", "title": "Parent task"}

        # Leaf task (no children) - same priority as parent
        leaf_task = MagicMock()
        leaf_task.id = "leaf-1"
        leaf_task.priority = 1  # Same priority as parent
        leaf_task.complexity_score = 3
        leaf_task.category = "Unit tests"
        leaf_task.to_brief.return_value = {"ref": "#2", "id": "leaf-1", "title": "Leaf task"}

        task_manager.list_ready_tasks.return_value = [parent_task, leaf_task]

        # Mock list_tasks to indicate parent has children, leaf doesn't
        def mock_list_tasks(parent_task_id=None, status=None, limit=None, project_id=None):
            if parent_task_id == "parent-1":
                return [MagicMock()]  # Has children
            return []  # No children

        task_manager.list_tasks.side_effect = mock_list_tasks

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        result = suggest(prefer_subtasks=True)

        # At same priority, leaf task wins due to leaf bonus + other bonuses
        assert result["suggestion"]["id"] == "leaf-1"

    def test_suggest_next_task_with_type_filter(self, mock_readiness_registry) -> None:
        """Test suggest_next_task with task_type filter."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        task_manager.list_ready_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        suggest(task_type="bug")

        task_manager.list_ready_tasks.assert_called_once_with(
            task_type="bug",
            limit=50,
            project_id="test-project-id",
        )

    def test_suggest_next_task_scoring(self, mock_readiness_registry) -> None:
        """Test that suggest_next_task uses correct scoring algorithm with priority dominance."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()

        # Low priority task with good attributes
        task_low_priority = MagicMock()
        task_low_priority.id = "low-priority"
        task_low_priority.priority = 3
        task_low_priority.complexity_score = 2
        task_low_priority.category = "Unit tests"
        task_low_priority.to_brief.return_value = {"ref": "#1", "id": "low-priority"}

        # High priority task with no extra attributes
        task_high_priority = MagicMock()
        task_high_priority.id = "high-priority"
        task_high_priority.priority = 1
        task_high_priority.complexity_score = None
        task_high_priority.category = None
        task_high_priority.to_brief.return_value = {"ref": "#2", "id": "high-priority"}

        task_manager.list_ready_tasks.return_value = [task_low_priority, task_high_priority]
        task_manager.list_tasks.return_value = []  # Both are leaf tasks

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        result = suggest()

        # Priority weight is 110 per level to ensure priority dominates over bonuses
        # High priority (1): (4-1)*110 + 25 (leaf) = 355
        # Low priority (3): (4-3)*110 + 25 (leaf) + 15 (complexity) + 10 (category) = 160
        # High priority wins despite fewer bonuses
        assert result["suggestion"]["id"] == "high-priority"

    def test_suggest_next_task_includes_recommended_skills(self, mock_readiness_registry) -> None:
        """Test that suggest_next_task includes recommended_skills based on task category."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_task.priority = 1
        mock_task.complexity_score = 3
        mock_task.category = "code"  # Should recommend code-related skills
        mock_task.status = "open"
        mock_task.to_brief.return_value = {
            "id": "task-1",
            "title": "Task with category",
            "category": "code",
        }

        task_manager.list_ready_tasks.return_value = [mock_task]
        task_manager.list_tasks.return_value = []  # No children = leaf task

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        result = suggest()

        assert result["suggestion"] is not None
        assert "recommended_skills" in result
        assert isinstance(result["recommended_skills"], list)
        # Code category should include gobby-tasks
        assert "gobby-tasks" in result["recommended_skills"]

    def test_suggest_next_task_no_skills_when_no_suggestion(self, mock_readiness_registry) -> None:
        """Test that recommended_skills is not included when no task is suggested."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        task_manager.list_ready_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        result = suggest()

        assert result["suggestion"] is None
        assert "recommended_skills" not in result


class TestReadinessEdgeCases:
    """Tests for edge cases in readiness detection."""

    def test_completed_dependencies_make_task_ready(self, mock_readiness_registry) -> None:
        """Test that tasks become ready when all blocking deps are completed."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()

        # Task with completed blockers should be ready
        ready_task = MagicMock()
        ready_task.to_brief.return_value = {"id": "task-1", "status": "open"}
        task_manager.list_ready_tasks.return_value = [ready_task]

        registry = create_readiness_registry(task_manager=task_manager)
        list_ready = registry.get_tool("list_ready_tasks")

        result = list_ready()

        # Task should appear in ready list
        assert result["count"] == 1

    def test_multiple_blockers_all_must_complete(self, mock_readiness_registry) -> None:
        """Test that a task with multiple blockers needs all to complete."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()

        # Task is still blocked if any blocker is not complete
        blocked_task = MagicMock()
        blocked_task.to_brief.return_value = {"id": "task-1", "status": "open"}
        task_manager.list_blocked_tasks.return_value = [blocked_task]

        registry = create_readiness_registry(task_manager=task_manager)
        list_blocked = registry.get_tool("list_blocked_tasks")

        result = list_blocked()

        assert result["count"] == 1

    def test_circular_dependency_handling(self, mock_readiness_registry) -> None:
        """Test handling of circular dependencies."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()

        # Tasks in a cycle should be blocked
        task_a = MagicMock()
        task_a.to_brief.return_value = {"id": "task-a", "status": "open"}
        task_b = MagicMock()
        task_b.to_brief.return_value = {"id": "task-b", "status": "open"}

        task_manager.list_blocked_tasks.return_value = [task_a, task_b]

        registry = create_readiness_registry(task_manager=task_manager)
        list_blocked = registry.get_tool("list_blocked_tasks")

        result = list_blocked()

        # Both tasks should appear as blocked
        assert result["count"] == 2

    def test_missing_dependency_task(self, mock_readiness_registry) -> None:
        """Test handling when a dependency references a non-existent task."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        # Task with missing dependency should still be in blocked list
        # or treated as ready depending on implementation
        task_manager.list_blocked_tasks.return_value = []
        task_manager.list_ready_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)
        list_ready = registry.get_tool("list_ready_tasks")
        list_blocked = registry.get_tool("list_blocked_tasks")

        ready_result = list_ready()
        blocked_result = list_blocked()

        # Should handle gracefully without errors
        assert "count" in ready_result
        assert "count" in blocked_result

    def test_default_limit_values(self, mock_readiness_registry) -> None:
        """Test default limit values for listing functions."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()
        task_manager.list_ready_tasks.return_value = []
        task_manager.list_blocked_tasks.return_value = []

        registry = create_readiness_registry(task_manager=task_manager)

        list_ready = registry.get_tool("list_ready_tasks")
        list_ready()
        # Default limit for ready tasks is 10
        assert task_manager.list_ready_tasks.call_args.kwargs["limit"] == 10

        list_blocked = registry.get_tool("list_blocked_tasks")
        list_blocked()
        # Default limit for blocked tasks is 20
        assert task_manager.list_blocked_tasks.call_args.kwargs["limit"] == 20


class TestSuggestNextTaskWithParentTaskId:
    """Tests for suggest_next_task with parent filter."""

    def test_suggest_next_task_with_parent_task_id(self, mock_readiness_registry) -> None:
        """suggest_next_task respects parent_task_id filter."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()

        # Create parent task
        parent_task = MagicMock()
        parent_task.id = "epic-1"
        parent_task.parent_task_id = None

        # Create child tasks (descendants of epic-1)
        child_task = MagicMock()
        child_task.id = "child-1"
        child_task.parent_task_id = "epic-1"
        child_task.priority = 2
        child_task.complexity_score = None
        child_task.category = None
        child_task.to_brief.return_value = {"id": "child-1", "title": "Child task"}

        # Create unrelated task (not a descendant)
        other_task = MagicMock()
        other_task.id = "other-1"
        other_task.parent_task_id = "other-epic"
        other_task.priority = 1  # Higher priority but outside scope
        other_task.to_brief.return_value = {"id": "other-1"}

        # list_ready_tasks returns both tasks
        task_manager.list_ready_tasks.return_value = [child_task, other_task]

        # list_tasks is called multiple times:
        # 1. Building descendant set: children of epic-1
        # 2. Building descendant set: children of child-1 (empty = leaf)
        # 3. Scoring: children of child-1 (to check if leaf)
        def list_tasks_side_effect(**kwargs):
            parent_id = kwargs.get("parent_task_id")
            if parent_id == "epic-1":
                return [child_task]
            return []  # No children for child-1

        task_manager.list_tasks.side_effect = list_tasks_side_effect

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        result = suggest(parent_task_id="epic-1")

        # Should only suggest child task, not other-1
        assert result["suggestion"] is not None
        assert result["suggestion"]["id"] == "child-1"

    def test_suggest_next_task_with_parent_task_id_no_descendants(
        self, mock_readiness_registry, monkeypatch
    ) -> None:
        """suggest_next_task returns parent task itself when it's a ready leaf task."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()

        # Create the parent task as a ready leaf task
        parent_task = MagicMock()
        parent_task.id = "epic-1"
        parent_task.status = "open"
        parent_task.type = "task"
        parent_task.priority = 2
        parent_task.configure_mock(complexity_score=None, category=None)
        parent_task.to_brief.return_value = {"ref": "#1", "id": "epic-1", "title": "Parent task"}

        # No ready tasks are descendants of the specified parent
        other_task = MagicMock()
        other_task.id = "other-1"
        other_task.parent_task_id = "other-epic"

        # Parent task must appear in list_ready_tasks for it to be considered ready
        task_manager.list_ready_tasks.return_value = [other_task, parent_task]
        task_manager.list_tasks.return_value = []  # No children of epic-1
        task_manager.get_task.return_value = parent_task
        task_manager.get_blocking_tasks.return_value = []  # No blockers

        # Monkeypatch resolve_task_id_for_mcp to pass through unchanged
        monkeypatch.setattr(
            "gobby.mcp_proxy.tools.tasks.resolve_task_id_for_mcp",
            lambda tm, tid, pid: tid,
        )

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        result = suggest(parent_task_id="epic-1")

        # Should return the parent task itself since it's a ready leaf task
        assert result["suggestion"] is not None
        assert result["suggestion"]["id"] == "epic-1"


class TestSuggestNextTaskWithSessionId:
    """Tests for suggest_next_task with session_id auto-scoping."""

    def test_suggest_next_task_with_session_id_scopes_to_session_task(
        self, mock_readiness_registry, monkeypatch
    ) -> None:
        """Test suggest_next_task auto-scopes when session_id has session_task variable."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry


        task_manager = MagicMock()

        # Create child task (descendant of session_task)
        child_task = MagicMock()
        child_task.id = "child-1"
        child_task.parent_task_id = "epic-1"
        child_task.priority = 2
        child_task.status = "open"
        child_task.configure_mock(complexity_score=None, category=None)
        child_task.to_brief.return_value = {"ref": "#1", "id": "child-1", "title": "Child task"}

        # Create unrelated task (not a descendant)
        other_task = MagicMock()
        other_task.id = "other-1"
        other_task.parent_task_id = "other-epic"
        other_task.priority = 1  # Higher priority but outside scope
        other_task.status = "open"
        other_task.configure_mock(complexity_score=None, category=None)
        other_task.to_brief.return_value = {"ref": "#2", "id": "other-1"}

        # list_ready_tasks returns both tasks
        task_manager.list_ready_tasks.return_value = [child_task, other_task]

        def list_tasks_side_effect(**kwargs):
            parent_id = kwargs.get("parent_task_id")
            status = kwargs.get("status")
            if status == "in_progress":
                return []  # No in_progress tasks
            if parent_id == "epic-1":
                return [child_task]
            return []

        task_manager.list_tasks.side_effect = list_tasks_side_effect

        # Patch SessionVariableManager.get_variables to return session_task
        monkeypatch.setattr(
            "gobby.workflows.state_manager.SessionVariableManager.get_variables",
            lambda self, sid: {"session_task": "epic-1"} if sid == "test-session-123" else {},
        )

        # Monkeypatch resolve_task_id_for_mcp to pass through unchanged
        monkeypatch.setattr(
            "gobby.mcp_proxy.tools.tasks.resolve_task_id_for_mcp",
            lambda tm, tid, pid: tid,
        )

        # Monkeypatch session_manager.resolve_session_reference to pass through unchanged
        monkeypatch.setattr(
            "gobby.storage.sessions.LocalSessionManager.resolve_session_reference",
            lambda self, sid, pid: sid,
        )

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        result = suggest(session_id="test-session-123")
        # Should only suggest child task due to session_task scoping
        assert result["suggestion"] is not None
        assert result["suggestion"]["id"] == "child-1"

    def test_suggest_next_task_session_id_ignored_if_parent_task_id_set(
        self, mock_readiness_registry, monkeypatch
    ) -> None:
        """If parent_task_id is explicit, session context is ignored."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()

        child_task = MagicMock()
        child_task.id = "child-of-explicit"
        child_task.parent_task_id = "explicit-parent"
        child_task.priority = 2
        child_task.status = "open"
        child_task.configure_mock(complexity_score=None, category=None)
        child_task.to_brief.return_value = {"id": "child-of-explicit"}

        task_manager.list_ready_tasks.return_value = [child_task]

        def list_tasks_side_effect(**kwargs):
            parent_id = kwargs.get("parent_task_id")
            status = kwargs.get("status")
            if status == "in_progress":
                return []
            if parent_id == "explicit-parent":
                return [child_task]
            return []

        task_manager.list_tasks.side_effect = list_tasks_side_effect

        # Patch SessionVariableManager - session_task is set but parent_id takes precedence
        monkeypatch.setattr(
            "gobby.workflows.state_manager.SessionVariableManager.get_variables",
            lambda self, sid: {"session_task": "epic-1"},  # Different from explicit-parent
        )

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        # parent_id takes precedence over session_id
        result = suggest(session_id="test-session", parent_task_id="explicit-parent")

        assert result["suggestion"] is not None
        assert result["suggestion"]["id"] == "child-of-explicit"

    def test_suggest_next_task_session_task_complete_when_closed(
        self, mock_readiness_registry, monkeypatch
    ) -> None:
        """Test suggest_next_task returns session_task_complete when scoped epic is closed."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry


        task_manager = MagicMock()

        # The epic is closed — no ready descendants
        closed_epic = MagicMock()
        closed_epic.id = "epic-1"
        closed_epic.status = "closed"

        task_manager.list_ready_tasks.return_value = []
        task_manager.list_tasks.return_value = []  # No descendants
        task_manager.get_task.return_value = closed_epic

        # Patch SessionVariableManager - session_task points to the closed epic
        monkeypatch.setattr(
            "gobby.workflows.state_manager.SessionVariableManager.get_variables",
            lambda self, sid: {"session_task": "epic-1"} if sid == "test-session-123" else {},
        )
        monkeypatch.setattr(
            "gobby.mcp_proxy.tools.tasks.resolve_task_id_for_mcp",
            lambda tm, tid, pid: tid,
        )
        monkeypatch.setattr(
            "gobby.storage.sessions.LocalSessionManager.resolve_session_reference",
            lambda self, sid, pid: sid,
        )

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        result = suggest(session_id="test-session-123")

        assert result["suggestion"] is None
        assert result["session_task_complete"] is True
        assert "complete" in result["reason"].lower()

    def test_suggest_next_task_session_id_star_does_not_scope(
        self, mock_readiness_registry, monkeypatch
    ) -> None:
        """Test session_task='*' does not scope (allows all tasks)."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        task_manager = MagicMock()

        task = MagicMock()
        task.id = "any-task"
        task.parent_task_id = None
        task.priority = 1
        task.status = "open"
        task.configure_mock(complexity_score=None, category=None)
        task.to_brief.return_value = {"id": "any-task"}

        task_manager.list_ready_tasks.return_value = [task]
        task_manager.list_tasks.return_value = []

        # Patch SessionVariableManager - session_task set to "*" (no scoping)
        monkeypatch.setattr(
            "gobby.workflows.state_manager.SessionVariableManager.get_variables",
            lambda self, sid: {"session_task": "*"},
        )

        registry = create_readiness_registry(task_manager=task_manager)
        suggest = registry.get_tool("suggest_next_task")

        result = suggest(session_id="test-session")

        # Should return any-task since "*" doesn't scope
        assert result["suggestion"] is not None
        assert result["suggestion"]["id"] == "any-task"


class TestIsDescendantOf:
    """Tests for is_descendant_of helper function."""

    def test_is_descendant_of_direct_child(self) -> None:
        """Test is_descendant_of for direct parent-child relationship."""
        from gobby.mcp_proxy.tools.task_readiness import is_descendant_of

        task_manager = MagicMock()

        # Child task
        child_task = MagicMock()
        child_task.parent_task_id = "parent-1"

        task_manager.get_task.return_value = child_task

        result = is_descendant_of(task_manager, "child-1", "parent-1")
        assert result is True

    def test_is_descendant_of_grandchild(self) -> None:
        """Test is_descendant_of for grandparent-grandchild relationship."""
        from gobby.mcp_proxy.tools.task_readiness import is_descendant_of

        task_manager = MagicMock()

        # Grandchild -> Child -> Parent
        grandchild = MagicMock()
        grandchild.parent_task_id = "child-1"

        child = MagicMock()
        child.parent_task_id = "parent-1"

        task_manager.get_task.side_effect = [grandchild, child]

        result = is_descendant_of(task_manager, "grandchild-1", "parent-1")
        assert result is True

    def test_is_descendant_of_self(self) -> None:
        """Test is_descendant_of returns True for same task."""
        from gobby.mcp_proxy.tools.task_readiness import is_descendant_of

        task_manager = MagicMock()

        result = is_descendant_of(task_manager, "task-1", "task-1")
        assert result is True

    def test_is_descendant_of_unrelated(self) -> None:
        """Test is_descendant_of returns False for unrelated tasks."""
        from gobby.mcp_proxy.tools.task_readiness import is_descendant_of

        task_manager = MagicMock()

        # Task with different parent
        task = MagicMock()
        task.parent_task_id = "other-parent"

        other_parent = MagicMock()
        other_parent.parent_task_id = None

        task_manager.get_task.side_effect = [task, other_parent]

        result = is_descendant_of(task_manager, "task-1", "not-my-parent")
        assert result is False

    def test_is_descendant_of_not_found(self) -> None:
        """Test is_descendant_of returns False when task not found."""
        from gobby.mcp_proxy.tools.task_readiness import is_descendant_of

        task_manager = MagicMock()
        task_manager.get_task.return_value = None

        result = is_descendant_of(task_manager, "missing-task", "parent-1")
        assert result is False


@pytest.fixture
def mock_readiness_registry():
    """Fixture providing mock dependencies for registry creation."""
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
        mock_ctx.return_value = {"id": "test-project-id"}
        yield mock_ctx
