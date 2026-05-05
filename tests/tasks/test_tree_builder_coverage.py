from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.tree_builder import TaskTreeBuilder

pytestmark = pytest.mark.unit


def _task(**kwargs) -> Task:
    status = kwargs.pop("status", "open")
    if status == "closed":
        kwargs.setdefault("closed_at", "now")
    elif status == "escalated":
        kwargs.setdefault("is_escalated", True)
        kwargs.setdefault("escalated_at", "now")
    return Task(**kwargs)


@pytest.fixture
def mock_task_manager():
    """Create a mock task manager."""
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    return manager


@pytest.fixture
def builder(mock_task_manager):
    """Create a TaskTreeBuilder instance."""
    return TaskTreeBuilder(
        task_manager=mock_task_manager, project_id="test-project", session_id="test-session"
    )


def test_build_simple_tree(builder, mock_task_manager) -> None:
    """Test building a simple tree with one node."""
    mock_task = _task(
        id="t1",
        project_id="p1",
        title="Root",
        seq_num=1,
        status="open",
        priority=2,
        task_type="epic",
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
    )
    mock_task_manager.create_task.return_value = mock_task
    mock_task_manager.get_task.return_value = mock_task

    tree = {"title": "Root Task", "task_type": "epic"}

    result = builder.build(tree)

    assert result.tasks_created == 1
    assert result.epic_ref == "#1"
    assert result.errors == []

    # TaskTreeBuilder implementation calls create_task with explicit kwargs
    # We verify that standard kwargs are passed correctly
    calls = mock_task_manager.create_task.call_args_list
    assert len(calls) == 1
    call_kwargs = calls[0].kwargs

    # Check essential arguments
    assert call_kwargs["title"] == "Root Task"
    assert call_kwargs["project_id"] == "test-project"
    assert call_kwargs["task_type"] == "epic"
    assert call_kwargs["parent_task_id"] is None
    assert call_kwargs["created_in_session_id"] == "test-session"


def test_build_nested_tree(builder, mock_task_manager) -> None:
    """Test building a tree with children."""
    root_task = _task(
        id="t1",
        project_id="p1",
        title="Root",
        seq_num=1,
        status="open",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    child_task = _task(
        id="t2",
        project_id="p1",
        title="Child",
        seq_num=2,
        status="open",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )

    # Setup side effects for create_task
    mock_task_manager.create_task.side_effect = [root_task, child_task]

    # Setup side effects for get_task
    def get_task_side_effect(task_id, project_id=None):
        if task_id == "t1":
            return root_task
        if task_id == "t2":
            return child_task
        return None

    mock_task_manager.get_task.side_effect = get_task_side_effect

    tree = {"title": "Root", "children": [{"title": "Child"}]}

    result = builder.build(tree)

    assert result.tasks_created == 2
    assert "#1" in result.task_refs
    assert "#2" in result.task_refs

    # Check hierarchy
    calls = mock_task_manager.create_task.call_args_list

    # First call (Root)
    assert calls[0].kwargs["title"] == "Root"
    assert calls[0].kwargs["parent_task_id"] is None

    # Second call (Child)
    assert calls[1].kwargs["title"] == "Child"
    assert calls[1].kwargs["parent_task_id"] == "t1"


@patch("gobby.storage.task_dependencies.TaskDependencyManager")
def test_dependency_resolution_by_title(MockDepManager, builder, mock_task_manager):
    """Test resolving dependencies by title."""
    t1 = _task(
        id="t1",
        project_id="p1",
        title="A",
        status="open",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    t2 = _task(
        id="t2",
        project_id="p1",
        title="B",
        status="open",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.create_task.side_effect = [t1, t2]

    # We mock get_task to return tasks so that refs can be built
    def get_task_side_effect(task_id, project_id=None):
        if task_id == "t1":
            return t1
        if task_id == "t2":
            return t2
        return None

    mock_task_manager.get_task.side_effect = get_task_side_effect

    mock_dep_manager = MockDepManager.return_value

    tree = {
        "title": "Root",  # t1
        "children": [
            {
                "title": "Child",  # t2
                "depends_on": ["Root"],
            }
        ],
    }

    result = builder.build(tree)

    assert result.errors == []
    mock_dep_manager.add_dependency.assert_called_with(
        task_id="t2", depends_on="t1", dep_type="blocks"
    )


@patch("gobby.storage.task_dependencies.TaskDependencyManager")
def test_dependency_resolution_by_index(MockDepManager, builder, mock_task_manager):
    """Test resolving dependencies by numeric sibling index."""
    root = _task(
        id="root",
        project_id="p1",
        title="Root",
        status="open",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    c1 = _task(
        id="c1",
        project_id="p1",
        title="Child 1",
        status="open",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    c2 = _task(
        id="c2",
        project_id="p1",
        title="Child 2",
        status="open",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )

    mock_task_manager.create_task.side_effect = [root, c1, c2]
    mock_dep_manager = MockDepManager.return_value

    tree = {
        "title": "Root",
        "children": [
            {"title": "Child 1"},  # Index 0
            {
                "title": "Child 2",  # Index 1
                "depends_on": [0],  # Depends on Child 1
            },
        ],
    }

    result = builder.build(tree)

    assert result.errors == []
    mock_dep_manager.add_dependency.assert_called_with(
        task_id="c2", depends_on="c1", dep_type="blocks"
    )


def test_missing_title_error(builder) -> None:
    """Test error handling for missing title."""
    tree = {
        "task_type": "epic",
        # Missing title
        "children": [],
    }

    result = builder.build(tree)

    assert result.tasks_created == 0
    assert len(result.errors) > 0
    assert "missing required 'title'" in result.errors[0]


def test_task_creation_failure(builder, mock_task_manager) -> None:
    """Test handling of task creation exceptions."""
    mock_task_manager.create_task.side_effect = Exception("DB Error")

    tree = {"title": "Fail Task"}

    result = builder.build(tree)

    assert result.tasks_created == 0
    assert len(result.errors) > 0
    assert "Failed to create task 'Fail Task'" in result.errors[0]


@patch("gobby.storage.task_dependencies.TaskDependencyManager")
def test_duplicate_title_handling(MockDepManager, builder, mock_task_manager):
    """Test handling of duplicate task titles."""
    t1 = _task(
        id="t1",
        project_id="p1",
        title="A",
        status="open",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    t2 = _task(
        id="t2",
        project_id="p1",
        title="A",
        status="open",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.create_task.side_effect = [t1, t2]

    tree = {"title": "A", "children": [{"title": "A"}]}

    result = builder.build(tree)

    # Should allow creation but log error/warning
    assert result.tasks_created == 2
    assert len(result.errors) > 0
    assert "Duplicate task title 'A'" in result.errors[0]
    # The map should update to the latest ID
    assert builder.get_id_for_title("A") == "t2"


@patch("gobby.storage.task_dependencies.TaskDependencyManager")
def test_invalid_dependency_format(MockDepManager, builder, mock_task_manager):
    """Test error handling for invalid dependency types."""
    mock_task_manager.create_task.return_value = _task(
        id="t1",
        project_id="p1",
        title="A",
        status="open",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )

    tree = {
        "title": "A",
        "depends_on": [
            123,
            {"invalid": "type"},
        ],  # Invalid index (no parent context for root) and invalid type
    }

    result = builder.build(tree)

    # 123 is treated as index lookup on None parent -> empty map -> not found
    assert len(result.errors) >= 2
    assert "Sibling index 123 not found" in result.errors[0]
    assert "Invalid dependency type dict" in result.errors[1]


@patch("gobby.storage.task_dependencies.TaskDependencyManager")
def test_dependency_not_found(MockDepManager, builder, mock_task_manager):
    """Test error handling for missing named dependency."""
    mock_task_manager.create_task.return_value = _task(
        id="t1",
        project_id="p1",
        title="A",
        status="open",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )

    tree = {"title": "A", "depends_on": ["NonExistent"]}

    result = builder.build(tree)

    assert len(result.errors) == 1
    assert "Dependency not found: 'NonExistent'" in result.errors[0]
