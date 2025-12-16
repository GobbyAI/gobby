import pytest
from datetime import datetime, timezone
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.task_dependencies import TaskDependencyManager, DependencyCycleError


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
    def test_add_dependency(self, task_manager, dep_manager, project_id):
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")  # T1 -> T2 (T1 depends on T2)

        dep = dep_manager.add_dependency(t1.id, t2.id, "blocks")
        assert dep.task_id == t1.id
        assert dep.depends_on == t2.id
        assert dep.dep_type == "blocks"

    def test_cycle_detection(self, task_manager, dep_manager, project_id):
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")
        t3 = task_manager.create_task(project_id, "T3")

        # T1 -> T2 -> T3
        dep_manager.add_dependency(t1.id, t2.id)
        dep_manager.add_dependency(t2.id, t3.id)

        # Try T3 -> T1 (Cycle)
        with pytest.raises(DependencyCycleError):
            dep_manager.add_dependency(t3.id, t1.id)

    def test_get_blockers_blocking(self, task_manager, dep_manager, project_id):
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

    def test_dependency_tree(self, task_manager, dep_manager, project_id):
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

    def test_check_cycles_global(self, task_manager, dep_manager, project_id):
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")

        dep_manager.add_dependency(t1.id, t2.id)

        # Manually insert back edge to force cycle
        now = datetime.now(timezone.utc).isoformat()
        with dep_manager.db.transaction() as conn:
            conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, 'blocks', ?)",
                (t2.id, t1.id, now),
            )

        cycles = dep_manager.check_cycles()
        assert len(cycles) > 0
        cycle_ids = set(cycles[0])
        assert t1.id in cycle_ids
        assert t2.id in cycle_ids

    def test_remove_dependency(self, task_manager, dep_manager, project_id):
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")

        dep_manager.add_dependency(t1.id, t2.id)
        assert len(dep_manager.get_blockers(t1.id)) == 1

        removed = dep_manager.remove_dependency(t1.id, t2.id)
        assert removed
        assert len(dep_manager.get_blockers(t1.id)) == 0

    def test_self_dependency_fails(self, task_manager, dep_manager, project_id):
        t1 = task_manager.create_task(project_id, "T1")
        with pytest.raises(ValueError, match="itself"):
            dep_manager.add_dependency(t1.id, t1.id)
