from datetime import UTC, datetime

import pytest

from gobby.storage.task_dependencies import DependencyCycleError, TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def dep_manager(temp_db):
    return TaskDependencyManager(temp_db)


@pytest.fixture
def project_id(sample_project):
    return sample_project["id"]


class TestTaskDependencyManager:
    def test_add_dependency(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")  # T1 -> T2 (T1 depends on T2)

        dep = dep_manager.add_dependency(t1.id, t2.id, "blocks")
        assert dep.task_id == t1.id
        assert dep.depends_on == t2.id
        assert dep.dep_type == "blocks"

    def test_cycle_detection(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")
        t3 = task_manager.create_task(project_id, "T3")

        # T1 -> T2 -> T3
        dep_manager.add_dependency(t1.id, t2.id)
        dep_manager.add_dependency(t2.id, t3.id)

        # Try T3 -> T1 (Cycle)
        with pytest.raises(DependencyCycleError):
            dep_manager.add_dependency(t3.id, t1.id)

    def test_get_blockers_blocking(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")

        # T1 needs T2 (T2 blocks T1)
        dep_manager.add_dependency(t1.id, t2.id)

        blockers = dep_manager.get_blockers(t1.id)
        assert len(blockers) == 1
        assert blockers[0].depends_on == t2.id

        blocking = dep_manager.get_blocking(t2.id)
        assert len(blocking) == 1
        assert blocking[0].task_id == t1.id

    def test_dependency_tree(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")
        t3 = task_manager.create_task(project_id, "T3")

        # T1 -> T2 -> T3
        dep_manager.add_dependency(t1.id, t2.id)
        dep_manager.add_dependency(t2.id, t3.id)

        tree = dep_manager.get_dependency_tree(t1.id, direction="blockers")
        # {id: T1, blockers: [{id: T2, blockers: [{id: T3, blockers: []}]}]}
        assert tree["id"] == t1.id
        assert len(tree["blockers"]) == 1
        assert tree["blockers"][0]["id"] == t2.id
        assert len(tree["blockers"][0]["blockers"]) == 1
        assert tree["blockers"][0]["blockers"][0]["id"] == t3.id

    def test_check_cycles_global(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")

        dep_manager.add_dependency(t1.id, t2.id)

        # Manually insert back edge to force cycle
        now = datetime.now(UTC).isoformat()
        with dep_manager.db.transaction() as conn:
            conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (%s, %s, 'blocks', %s)",
                (t2.id, t1.id, now),
            )

        cycles = dep_manager.check_cycles()
        assert len(cycles) > 0
        cycle_ids = set(cycles[0])
        assert t1.id in cycle_ids
        assert t2.id in cycle_ids

    def test_remove_dependency(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")

        dep_manager.add_dependency(t1.id, t2.id)
        assert len(dep_manager.get_blockers(t1.id)) == 1

        removed = dep_manager.remove_dependency(t1.id, t2.id)
        assert removed
        assert len(dep_manager.get_blockers(t1.id)) == 0

    def test_self_dependency_fails(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        with pytest.raises(ValueError, match="itself"):
            dep_manager.add_dependency(t1.id, t1.id)

    def test_to_dict(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")
        dep = dep_manager.add_dependency(t1.id, t2.id, "related")

        data = dep.to_dict()
        assert data["task_id"] == t1.id
        assert data["depends_on"] == t2.id
        assert data["dep_type"] == "related"
        assert "created_at" in data
        assert "id" in data

    def test_get_all_dependencies(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")
        t3 = task_manager.create_task(project_id, "T3")

        # T1 -> T2 (blocks)
        # T1 -> T3 (related)
        dep_manager.add_dependency(t1.id, t2.id, "blocks")
        dep_manager.add_dependency(t1.id, t3.id, "related")

        deps = dep_manager.get_all_dependencies(t1.id)
        assert len(deps) == 2
        dw_ids = {d.depends_on for d in deps}
        assert t2.id in dw_ids
        assert t3.id in dw_ids

    def test_dependency_tree_blocking_and_both(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")
        t3 = task_manager.create_task(project_id, "T3")

        # T1 -> T2 -> T3
        dep_manager.add_dependency(t1.id, t2.id)
        dep_manager.add_dependency(t2.id, t3.id)

        # Test blocking direction (downstream from T3's perspective)
        # T3 blocks T2, T2 blocks T1
        tree_blocking = dep_manager.get_dependency_tree(t3.id, direction="blocking")
        assert tree_blocking["id"] == t3.id
        assert len(tree_blocking["blocking"]) == 1
        assert tree_blocking["blocking"][0]["id"] == t2.id
        assert tree_blocking["blocking"][0]["blocking"][0]["id"] == t1.id

        # Test 'both' direction from T2 (middle node)
        # T2 is blocked by T3, and blocks T1 ??? Wait, T1 depends on T2 depends on T3
        # T1 -> T2 -> T3
        # T2 blockers: T3 (T2 depends on T3)
        # T2 blocking: T1 (T1 depends on T2)
        tree_both = dep_manager.get_dependency_tree(t2.id, direction="both")
        assert tree_both["id"] == t2.id

        # Check blockers (upstream)
        assert len(tree_both["blockers"]) == 1
        assert tree_both["blockers"][0]["id"] == t3.id

        # Check blocking (downstream)
        assert len(tree_both["blocking"]) == 1
        assert tree_both["blocking"][0]["id"] == t1.id

    def test_dependency_tree_max_depth(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")
        t3 = task_manager.create_task(project_id, "T3")

        dep_manager.add_dependency(t1.id, t2.id)
        dep_manager.add_dependency(t2.id, t3.id)

        # Depth 0 -> truncated immediately
        tree_0 = dep_manager.get_dependency_tree(t1.id, max_depth=0)
        assert tree_0.get("_truncated") is True
        assert "blockers" not in tree_0

        # Depth 1 -> sees T2, but T2's children truncated?
        # Actually max_depth determines recursion.
        # Level 1 call: max_depth=1. recurse with max_depth=0.
        tree_1 = dep_manager.get_dependency_tree(t1.id, max_depth=1)
        assert len(tree_1["blockers"]) == 1
        child_node = tree_1["blockers"][0]
        assert child_node["id"] == t2.id
        # The child node was generated with max_depth=0, so it should be truncated and have no blockers
        assert child_node.get("_truncated") is True
        assert "blockers" not in child_node
