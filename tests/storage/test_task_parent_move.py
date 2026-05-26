"""Tests for parent task move behavior."""

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase):
    database = temp_db
    with database.transaction() as conn:
        conn.execute("INSERT INTO projects (id, name) VALUES (?, ?)", ("p1", "test_project"))
    return database


@pytest.fixture
def manager(db):
    return LocalTaskManager(db)


class TestParentTaskMove:
    """Test that children follow their parent when parent is moved."""

    def test_move_parent_children_follow(self, manager) -> None:
        """When parent B is moved from A to D, children of B should still be B's children."""
        # Create hierarchy: A -> B -> C
        a = manager.create_task(project_id="p1", title="Epic A", task_type="epic")
        b = manager.create_task(
            project_id="p1", title="Feature B", task_type="feature", parent_task_id=a.id
        )
        c = manager.create_task(
            project_id="p1", title="Task C", task_type="task", parent_task_id=b.id
        )

        # Create new parent D
        d = manager.create_task(project_id="p1", title="Epic D", task_type="epic")

        # Move B from A to D
        manager.update_task(b.id, parent_task_id=d.id)

        # Verify B is now under D
        b_updated = manager.get_task(b.id)
        assert b_updated.parent_task_id == d.id

        # Verify C is still under B (this is the key assertion)
        c_updated = manager.get_task(c.id)
        assert c_updated.parent_task_id == b.id

        # Verify A has no children anymore
        a_children = manager.list_tasks(parent_task_id=a.id)
        assert len(a_children) == 0

        # Verify D has B as child
        d_children = manager.list_tasks(parent_task_id=d.id)
        assert len(d_children) == 1
        assert d_children[0].id == b.id

        # Verify B still has C as child
        b_children = manager.list_tasks(parent_task_id=b.id)
        assert len(b_children) == 1
        assert b_children[0].id == c.id

    def test_move_to_root_children_follow(self, manager) -> None:
        """When parent B is moved to root (cleared parent), children should follow."""
        # Create hierarchy: A -> B -> C
        a = manager.create_task(project_id="p1", title="Epic A", task_type="epic")
        b = manager.create_task(
            project_id="p1", title="Feature B", task_type="feature", parent_task_id=a.id
        )
        c = manager.create_task(
            project_id="p1", title="Task C", task_type="task", parent_task_id=b.id
        )

        # Move B to root (clear parent)
        manager.update_task(b.id, parent_task_id=None)

        # Verify B is now a root task
        b_updated = manager.get_task(b.id)
        assert b_updated.parent_task_id is None

        # Verify C is still under B
        c_updated = manager.get_task(c.id)
        assert c_updated.parent_task_id == b.id

        # B should still have C as child
        b_children = manager.list_tasks(parent_task_id=b.id)
        assert len(b_children) == 1
        assert b_children[0].id == c.id

    def test_deep_hierarchy_move(self, manager) -> None:
        """Moving a task with deep hierarchy should preserve entire subtree."""
        # Create hierarchy: A -> B -> C -> D -> E
        a = manager.create_task(project_id="p1", title="Epic A", task_type="epic")
        b = manager.create_task(project_id="p1", title="B", parent_task_id=a.id)
        c = manager.create_task(project_id="p1", title="C", parent_task_id=b.id)
        d = manager.create_task(project_id="p1", title="D", parent_task_id=c.id)
        e = manager.create_task(project_id="p1", title="E", parent_task_id=d.id)

        # Create new root
        x = manager.create_task(project_id="p1", title="Epic X", task_type="epic")

        # Move B (with C, D, E underneath) to X
        manager.update_task(b.id, parent_task_id=x.id)

        # Verify entire subtree maintained
        b_updated = manager.get_task(b.id)
        c_updated = manager.get_task(c.id)
        d_updated = manager.get_task(d.id)
        e_updated = manager.get_task(e.id)

        assert b_updated.parent_task_id == x.id
        assert c_updated.parent_task_id == b.id
        assert d_updated.parent_task_id == c.id
        assert e_updated.parent_task_id == d.id
