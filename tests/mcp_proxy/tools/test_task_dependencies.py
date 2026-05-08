"""
Tests for task_dependencies.py MCP tools module.

This file tests the dependency management tools that will be extracted
from tasks.py into task_dependencies.py using Strangler Fig pattern.

RED PHASE: These tests will fail initially because task_dependencies.py
does not exist yet. The module will be created in the green phase.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestAddDependency:
    """Tests for add_dependency MCP tool."""

    def test_add_dependency_success(self, mock_task_registry) -> None:
        """Test successful dependency addition."""
        # Import from future module location
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        task_manager = MagicMock()
        dep_manager = MagicMock()
        dep_manager.add_dependency.return_value = MagicMock(
            task_id="task-1",
            depends_on="task-2",
            dep_type="blocks",
        )

        registry = create_dependency_registry(
            task_manager=task_manager,
            dep_manager=dep_manager,
        )

        # Get the add_dependency tool
        add_dep = registry.get_tool("add_dependency")
        result = add_dep(task_id="task-1", depends_on="task-2", dep_type="blocks")

        assert result["added"] is True
        assert result["task_id"] == "task-1"
        assert result["depends_on"] == "task-2"
        dep_manager.add_dependency.assert_called_once_with("task-1", "task-2", "blocks")

    def test_add_dependency_default_type(self, mock_task_registry) -> None:
        """Test add_dependency uses 'blocks' as default dep_type."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)

        add_dep = registry.get_tool("add_dependency")
        add_dep(task_id="task-1", depends_on="task-2")

        # Should default to "blocks"
        dep_manager.add_dependency.assert_called_with("task-1", "task-2", "blocks")
        assert dep_manager.add_dependency.call_count >= 1
        assert dep_manager.add_dependency.call_args is not None

    def test_add_dependency_cycle_error(self, mock_task_registry) -> None:
        """Test add_dependency returns error on cycle detection."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.add_dependency.side_effect = ValueError("Cycle detected")

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        add_dep = registry.get_tool("add_dependency")

        result = add_dep(task_id="task-1", depends_on="task-2")

        assert "error" in result
        assert "Cycle" in result["error"]

    def test_add_dependency_self_reference_error(self, mock_task_registry) -> None:
        """Test add_dependency rejects self-dependencies."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.add_dependency.side_effect = ValueError("Cannot depend on itself")

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        add_dep = registry.get_tool("add_dependency")

        result = add_dep(task_id="task-1", depends_on="task-1")

        assert "error" in result


class TestRemoveDependency:
    """Tests for remove_dependency MCP tool."""

    def test_remove_dependency_success(self, mock_task_registry) -> None:
        """Test successful dependency removal."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.remove_dependency.return_value = True

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        remove_dep = registry.get_tool("remove_dependency")

        result = remove_dep(task_id="task-1", depends_on="task-2")

        assert result["removed"] is True
        assert result["task_id"] == "task-1"
        assert result["depends_on"] == "task-2"

    def test_remove_dependency_not_found(self, mock_task_registry) -> None:
        """Test removing non-existent dependency."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.remove_dependency.return_value = False

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        remove_dep = registry.get_tool("remove_dependency")

        result = remove_dep(task_id="task-1", depends_on="task-2")

        # Should still return removed: True per current behavior
        assert result["removed"] is True


class TestGetDependencyTree:
    """Tests for get_dependency_tree MCP tool."""

    def test_get_dependency_tree_both_directions(self, mock_task_registry) -> None:
        """Test getting full dependency tree."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.get_dependency_tree.return_value = {
            "id": "task-1",
            "blockers": [{"id": "task-2", "blockers": []}],
            "blocking": [{"id": "task-3", "blocking": []}],
        }

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        get_tree = registry.get_tool("get_dependency_tree")

        result = get_tree(task_id="task-1", direction="both")

        assert "blockers" in result
        assert "blocking" in result

    def test_get_dependency_tree_blockers_only(self, mock_task_registry) -> None:
        """Test getting only blockers (upstream dependencies)."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.get_dependency_tree.return_value = {
            "id": "task-1",
            "blockers": [{"id": "task-2"}],
            "blocking": [{"id": "task-3"}],
        }

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        get_tree = registry.get_tool("get_dependency_tree")

        result = get_tree(task_id="task-1", direction="blockers")

        assert "blockers" in result
        assert "blocking" not in result

    def test_get_dependency_tree_blocking_only(self, mock_task_registry) -> None:
        """Test getting only blocking (downstream dependents)."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.get_dependency_tree.return_value = {
            "id": "task-1",
            "blockers": [{"id": "task-2"}],
            "blocking": [{"id": "task-3"}],
        }

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        get_tree = registry.get_tool("get_dependency_tree")

        result = get_tree(task_id="task-1", direction="blocking")

        assert "blocking" in result
        assert "blockers" not in result

    def test_get_dependency_tree_deep_nesting(self, mock_task_registry) -> None:
        """Test dependency tree with deep nesting."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.get_dependency_tree.return_value = {
            "id": "task-1",
            "blockers": [
                {
                    "id": "task-2",
                    "blockers": [{"id": "task-3", "blockers": [{"id": "task-4", "blockers": []}]}],
                }
            ],
        }

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        get_tree = registry.get_tool("get_dependency_tree")

        result = get_tree(task_id="task-1", direction="blockers")

        # Verify deep structure preserved
        assert result["blockers"][0]["id"] == "task-2"
        assert result["blockers"][0]["blockers"][0]["id"] == "task-3"


class TestCheckDependencyCycles:
    """Tests for check_dependency_cycles MCP tool."""

    def test_check_cycles_none_found(self, mock_task_registry) -> None:
        """Test when no cycles exist."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.check_cycles.return_value = []

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        check_cycles = registry.get_tool("check_dependency_cycles")

        result = check_cycles()

        assert result["has_cycles"] is False
        assert "cycles" not in result or result.get("cycles") == []

    def test_check_cycles_found(self, mock_task_registry) -> None:
        """Test when cycles are detected."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.check_cycles.return_value = [["task-1", "task-2", "task-1"]]

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        check_cycles = registry.get_tool("check_dependency_cycles")

        result = check_cycles()

        assert result["has_cycles"] is True
        assert "cycles" in result
        assert len(result["cycles"]) == 1

    def test_check_cycles_multiple(self, mock_task_registry) -> None:
        """Test detection of multiple cycles."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.check_cycles.return_value = [
            ["task-1", "task-2", "task-1"],
            ["task-3", "task-4", "task-5", "task-3"],
        ]

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        check_cycles = registry.get_tool("check_dependency_cycles")

        result = check_cycles()

        assert result["has_cycles"] is True
        assert len(result["cycles"]) == 2


class TestEdgeCases:
    """Tests for edge cases in dependency management."""

    def test_empty_dependency_tree(self, mock_task_registry) -> None:
        """Test task with no dependencies."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        dep_manager.get_dependency_tree.return_value = {
            "id": "task-1",
            "blockers": [],
            "blocking": [],
        }

        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        get_tree = registry.get_tool("get_dependency_tree")

        result = get_tree(task_id="task-1")

        assert result.get("blockers", []) == []
        assert result.get("blocking", []) == []

    def test_dependency_type_variations(self, mock_task_registry) -> None:
        """Test different dependency types."""
        from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry

        dep_manager = MagicMock()
        registry = create_dependency_registry(task_manager=MagicMock(), dep_manager=dep_manager)
        add_dep = registry.get_tool("add_dependency")

        # Test each dep_type
        for dep_type in ["blocks", "discovered-from", "related"]:
            dep_manager.reset_mock()
            add_dep(task_id="task-1", depends_on="task-2", dep_type=dep_type)
            dep_manager.add_dependency.assert_called_with("task-1", "task-2", dep_type)
            assert dep_manager.add_dependency.call_count >= 1
            assert dep_manager.add_dependency.call_args is not None


@pytest.fixture
def mock_task_registry():
    """Fixture providing mock dependencies for registry creation."""
    with patch("gobby.mcp_proxy.tools.task_dependencies.get_current_project_id") as mock_proj:
        mock_proj.return_value = "test-project-id"
        yield mock_proj
