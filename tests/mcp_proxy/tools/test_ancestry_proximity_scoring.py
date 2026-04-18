"""Tests for ancestry-based proximity scoring in suggest_next_task.

Tests verify that tasks closer to the current in_progress task in the
hierarchy get higher scores, encouraging focused work within a branch.
"""

from unittest.mock import patch

import pytest

from gobby.mcp_proxy.tools.task_readiness import (
    _compute_proximity_boost,
    _get_ancestry_chain,
    create_readiness_registry,
)
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db):
    """Create a task manager with the shared temp database."""
    return LocalTaskManager(temp_db)


@pytest.fixture
def project_id(sample_project):
    """Get project ID from sample project fixture."""
    return sample_project["id"]


class TestGetAncestryChain:
    """Tests for _get_ancestry_chain helper function."""

    def test_root_task_returns_single_element(self, task_manager, project_id) -> None:
        """Root tasks with no parent return just themselves."""
        task = task_manager.create_task(project_id, "Root task", task_type="epic")

        chain = _get_ancestry_chain(task_manager, task.id)

        assert chain == [task.id]

    def test_child_task_returns_parent_chain(self, task_manager, project_id) -> None:
        """Child tasks return themselves and all ancestors."""
        parent = task_manager.create_task(project_id, "Parent", task_type="epic")
        child = task_manager.create_task(
            project_id, "Child", task_type="task", parent_task_id=parent.id
        )

        chain = _get_ancestry_chain(task_manager, child.id)

        assert chain == [child.id, parent.id]

    def test_deep_hierarchy_returns_full_chain(self, task_manager, project_id) -> None:
        """Deep hierarchies return the complete ancestry chain."""
        grandparent = task_manager.create_task(project_id, "Grandparent", task_type="epic")
        parent = task_manager.create_task(
            project_id, "Parent", task_type="feature", parent_task_id=grandparent.id
        )
        child = task_manager.create_task(
            project_id, "Child", task_type="task", parent_task_id=parent.id
        )
        grandchild = task_manager.create_task(
            project_id, "Grandchild", task_type="task", parent_task_id=child.id
        )

        chain = _get_ancestry_chain(task_manager, grandchild.id)

        assert chain == [grandchild.id, child.id, parent.id, grandparent.id]

    def test_nonexistent_task_returns_empty(self, task_manager, project_id) -> None:
        """Non-existent task IDs return empty chain."""
        chain = _get_ancestry_chain(task_manager, "gt-nonexistent")

        assert chain == []


class TestComputeProximityBoost:
    """Tests for _compute_proximity_boost helper function."""

    def test_same_task_gets_max_boost(self) -> None:
        """Same task as active gets maximum boost."""
        task_chain = ["gt-a"]
        active_chain = ["gt-a"]

        boost = _compute_proximity_boost(task_chain, active_chain)

        assert boost == 50  # max boost for depth 0

    def test_child_of_active_task(self) -> None:
        """Child of active task gets high boost."""
        # Task is child, active is parent
        task_chain = ["gt-child", "gt-parent"]
        active_chain = ["gt-parent"]

        boost = _compute_proximity_boost(task_chain, active_chain)

        assert boost == 50  # depth 0 from common ancestor (parent)

    def test_sibling_tasks(self) -> None:
        """Sibling tasks (same parent) get moderate boost."""
        # Both share parent as common ancestor
        task_chain = ["gt-sibling1", "gt-parent"]
        active_chain = ["gt-sibling2", "gt-parent"]

        boost = _compute_proximity_boost(task_chain, active_chain)

        # Distance: task->parent (1) + active->parent (1) = 2, but we use
        # depth from common ancestor to task, which is 1
        assert boost == 40  # depth 1: 50 - 10 = 40

    def test_cousin_tasks(self) -> None:
        """Cousin tasks (same grandparent) get lower boost."""
        task_chain = ["gt-cousin1", "gt-uncle", "gt-grandparent"]
        active_chain = ["gt-cousin2", "gt-aunt", "gt-grandparent"]

        boost = _compute_proximity_boost(task_chain, active_chain)

        # Common ancestor is grandparent, task is 2 levels below
        assert boost == 30  # depth 2: 50 - 20 = 30

    def test_distant_relatives_get_minimum_boost(self) -> None:
        """Tasks 5+ levels from common ancestor get no boost."""
        task_chain = ["gt-1", "gt-2", "gt-3", "gt-4", "gt-5", "gt-root"]
        active_chain = ["gt-other", "gt-root"]

        boost = _compute_proximity_boost(task_chain, active_chain)

        # Depth 5: 50 - 50 = 0
        assert boost == 0

    def test_no_common_ancestor_returns_zero(self) -> None:
        """Tasks with no common ancestor get zero boost."""
        task_chain = ["gt-a", "gt-b", "gt-c"]
        active_chain = ["gt-x", "gt-y", "gt-z"]

        boost = _compute_proximity_boost(task_chain, active_chain)

        assert boost == 0

    def test_empty_chains_return_zero(self) -> None:
        """Empty chains return zero boost."""
        assert _compute_proximity_boost([], ["gt-a"]) == 0
        assert _compute_proximity_boost(["gt-a"], []) == 0
        assert _compute_proximity_boost([], []) == 0


def _claim_task(task_manager: LocalTaskManager, project_id: str, task_id: str) -> None:
    session_manager = LocalSessionManager(task_manager.db)
    session = session_manager.register(
        external_id="readiness-ext",
        machine_id="test-machine",
        source="codex",
        project_id=project_id,
    )
    task_manager.claim_task(task_id, session.id)


class TestSuggestNextTaskProximityScoring:
    """Integration tests for proximity scoring in suggest_next_task."""

    @pytest.mark.asyncio
    async def test_prefers_task_in_same_branch(self, task_manager, project_id):
        """suggest_next_task prefers tasks in the same branch as in_progress task."""
        # Create two separate branches
        epic_a = task_manager.create_task(project_id, "Epic A", task_type="epic")
        task_a1 = task_manager.create_task(
            project_id, "Task A1", task_type="task", parent_task_id=epic_a.id
        )
        task_a2 = task_manager.create_task(
            project_id, "Task A2", task_type="task", parent_task_id=epic_a.id
        )

        epic_b = task_manager.create_task(project_id, "Epic B", task_type="epic")
        task_manager.create_task(
            project_id, "Task B1", task_type="task", parent_task_id=epic_b.id
        )  # Creates competing task in different branch

        # Set task_a1 as in_progress
        _claim_task(task_manager, project_id, task_a1.id)

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": project_id}
            registry = create_readiness_registry(task_manager)
            result = await registry.call("suggest_next_task", {})

        # Should suggest task_a2 (sibling) over task_b1 (different branch)
        assert result["suggestion"]["id"] == task_a2.id

    @pytest.mark.asyncio
    async def test_child_of_in_progress_preferred_over_sibling(self, task_manager, project_id):
        """Children of in_progress task get higher boost than siblings."""
        parent = task_manager.create_task(project_id, "Parent", task_type="feature")
        task_manager.create_task(
            project_id, "Sibling", task_type="task", parent_task_id=parent.id
        )  # Creates sibling to compare against
        in_progress = task_manager.create_task(
            project_id, "In Progress", task_type="task", parent_task_id=parent.id
        )
        child_of_active = task_manager.create_task(
            project_id, "Child of Active", task_type="task", parent_task_id=in_progress.id
        )

        _claim_task(task_manager, project_id, in_progress.id)

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": project_id}
            registry = create_readiness_registry(task_manager)
            result = await registry.call("suggest_next_task", {})

        # Child of in_progress gets +50, sibling gets +40
        assert result["suggestion"]["id"] == child_of_active.id

    @pytest.mark.asyncio
    async def test_no_boost_when_no_in_progress_task(self, task_manager, project_id):
        """When no task is in_progress, no proximity boost is applied."""
        epic = task_manager.create_task(project_id, "Epic", task_type="epic")
        task1 = task_manager.create_task(
            project_id, "High Priority", task_type="task", parent_task_id=epic.id, priority=1
        )
        task_manager.create_task(
            project_id, "Low Priority", task_type="task", parent_task_id=epic.id, priority=3
        )  # Lower priority task for comparison

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": project_id}
            registry = create_readiness_registry(task_manager)
            result = await registry.call("suggest_next_task", {})

        # Without proximity boost, priority wins
        assert result["suggestion"]["id"] == task1.id

    @pytest.mark.asyncio
    async def test_priority_dominates_over_proximity(self, task_manager, project_id):
        """Priority takes precedence over proximity - high priority task wins even without proximity."""
        # Create two branches
        epic_a = task_manager.create_task(project_id, "Epic A", task_type="epic")
        task_a1 = task_manager.create_task(
            project_id, "Task A1 (medium)", task_type="task", parent_task_id=epic_a.id, priority=2
        )
        task_manager.create_task(
            project_id, "Task A2 (low)", task_type="task", parent_task_id=epic_a.id, priority=3
        )

        epic_b = task_manager.create_task(project_id, "Epic B", task_type="epic")
        task_b1 = task_manager.create_task(
            project_id, "Task B1 (high)", task_type="task", parent_task_id=epic_b.id, priority=1
        )  # High priority but different branch

        # Set task_a1 as in_progress
        _claim_task(task_manager, project_id, task_a1.id)

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": project_id}
            registry = create_readiness_registry(task_manager)
            result = await registry.call("suggest_next_task", {})

        # Priority uses weight of 110 per level to ensure it dominates over bonuses
        # task_b1: (4-1)*110 = 330 + leaf 25 = 355 (no proximity)
        # task_a2: (4-3)*110 = 110 + leaf 25 + proximity ~50 = 185
        # task_b1 wins due to higher priority despite task_a2's proximity boost
        assert result["suggestion"]["id"] == task_b1.id

    @pytest.mark.asyncio
    async def test_different_tree_gets_no_proximity_boost(self, task_manager, project_id):
        """Tasks in completely separate trees get no proximity boost."""
        # Two completely separate task trees (flat, no parent epics to avoid complexity)
        tree1_task1 = task_manager.create_task(
            project_id, "Tree 1 Task 1", task_type="task", priority=3
        )
        task_manager.create_task(
            project_id, "Tree 1 Task 2", task_type="task", priority=3
        )  # Second task in tree 1

        tree2_task = task_manager.create_task(
            project_id, "Tree 2 Task", task_type="task", priority=1
        )

        # Set tree1_task1 as in_progress
        _claim_task(task_manager, project_id, tree1_task1.id)

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": project_id}
            registry = create_readiness_registry(task_manager)
            result = await registry.call("suggest_next_task", {})

        # tree1_task2 has NO proximity boost since tree1_task1 is its sibling but they
        # have no common parent. tree2_task also has no proximity boost.
        # Since tree2_task has higher priority (1 vs 3), it should win.
        # tree1_task2: priority 3 (+10) + leaf (+25) = 35
        # tree2_task: priority 1 (+30) + leaf (+25) = 55
        # tree2_task wins due to higher priority
        assert result["suggestion"]["id"] == tree2_task.id

    @pytest.mark.asyncio
    async def test_reason_includes_proximity_when_boosted(self, task_manager, project_id):
        """The reason field mentions proximity when it contributed to selection."""
        epic = task_manager.create_task(project_id, "Epic", task_type="epic")
        task1 = task_manager.create_task(
            project_id, "Task 1", task_type="task", parent_task_id=epic.id
        )
        task_manager.create_task(
            project_id, "Task 2", task_type="task", parent_task_id=epic.id
        )  # Creates sibling task

        _claim_task(task_manager, project_id, task1.id)

        with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": project_id}
            registry = create_readiness_registry(task_manager)
            result = await registry.call("suggest_next_task", {})

        assert "proximity" in result["reason"].lower() or "same branch" in result["reason"].lower()
