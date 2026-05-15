"""Tests for condition helper functions used in rule engine expressions."""

from __future__ import annotations

from uuid import UUID

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.condition_helpers import (
    _normalize_task_id,
    is_task_complete,
    task_needs_human_review,
    task_tree_complete,
    task_type_in,
)

pytestmark = pytest.mark.unit


def _manager(temp_db) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


def _task(manager: LocalTaskManager, sample_project: dict, **kwargs):
    title = kwargs.pop("title", "Condition helper task")
    return manager.create_task(project_id=sample_project["id"], title=title, **kwargs)


def _start_development_stage(manager: LocalTaskManager, task_id: str) -> None:
    manager.initialize_task_manifest(task_id)
    manager.stage_states.start_stage(task_id, "development", by_session_id=None)


def _seq_ref(task) -> str:
    assert task.seq_num is not None
    return f"#{task.seq_num}"


class TestNormalizeTaskId:
    def test_int_to_hash_format(self) -> None:
        assert _normalize_task_id(9438) == "#9438"

    def test_zero(self) -> None:
        assert _normalize_task_id(0) == "#0"

    def test_string_passthrough(self) -> None:
        assert _normalize_task_id("#9438") == "#9438"

    def test_uuid_passthrough(self) -> None:
        uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert _normalize_task_id(uuid) == uuid


class TestIsTaskComplete:
    def test_closed_is_complete(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)

        closed = manager.close_task(task.id, force=True)

        assert not hasattr(closed, "status")
        assert is_task_complete(closed) is True

    def test_ready_is_not_complete(self, temp_db, sample_project) -> None:
        task = _task(_manager(temp_db), sample_project)
        assert is_task_complete(task) is False

    def test_in_progress_is_not_complete(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        _start_development_stage(manager, task.id)

        assert is_task_complete(manager.get_task(task.id)) is False

    def test_needs_review_is_not_complete(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        _start_development_stage(manager, task.id)
        reviewed = manager.submit_for_review(task.id)

        assert is_task_complete(reviewed) is False

    def test_escalated_is_not_complete(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        escalated = manager.escalate_task(task.id, reason="needs human")

        assert is_task_complete(escalated) is False


class TestTaskTreeCompleteIntHandling:
    def test_int_task_id_closed(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        manager.close_task(task.id, force=True)

        assert task_tree_complete(manager, task.seq_num) is True

    def test_int_task_id_open(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)

        assert task_tree_complete(manager, task.seq_num) is False

    def test_int_task_id_not_found(self, temp_db) -> None:
        assert task_tree_complete(_manager(temp_db), 9438) is False

    def test_int_does_not_raise_type_error(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)

        assert task_tree_complete(manager, task.seq_num) is False


class TestTaskTreeCompleteString:
    def test_string_task_id_closed(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        manager.close_task(task.id, force=True)

        assert task_tree_complete(manager, task.id) is True

    def test_string_task_id_open(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)

        assert task_tree_complete(manager, task.id) is False


class TestTaskTreeCompleteList:
    def test_list_of_strings_all_closed(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        first = _task(manager, sample_project, title="First")
        second = _task(manager, sample_project, title="Second")
        manager.close_task(first.id, force=True)
        manager.close_task(second.id, force=True)

        assert task_tree_complete(manager, [first.id, second.id]) is True

    def test_list_of_ints_all_closed(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        first = _task(manager, sample_project, title="First")
        second = _task(manager, sample_project, title="Second")
        manager.close_task(first.id, force=True)
        manager.close_task(second.id, force=True)

        assert task_tree_complete(manager, [first.seq_num, second.seq_num]) is True

    def test_mixed_list(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        first = _task(manager, sample_project, title="First")
        second = _task(manager, sample_project, title="Second")
        manager.close_task(first.id, force=True)
        manager.close_task(second.id, force=True)

        assert task_tree_complete(manager, [first.id, second.seq_num]) is True

    def test_list_one_open(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        first = _task(manager, sample_project, title="First")
        second = _task(manager, sample_project, title="Second")
        manager.close_task(first.id, force=True)

        assert task_tree_complete(manager, [first.seq_num, second.seq_num]) is False


class TestTaskTreeCompleteEdgeCases:
    def test_none_returns_true(self, temp_db) -> None:
        assert task_tree_complete(_manager(temp_db), None) is True

    def test_no_task_manager_returns_false(self) -> None:
        assert task_tree_complete(None, "#1") is False

    def test_subtree_all_closed(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        parent = _task(manager, sample_project, title="Parent")
        child_a = _task(manager, sample_project, title="Child A", parent_task_id=parent.id)
        child_b = _task(manager, sample_project, title="Child B", parent_task_id=parent.id)
        manager.close_task(child_a.id, force=True)
        manager.close_task(child_b.id, force=True)

        assert task_tree_complete(manager, parent.id) is True

    def test_subtree_one_open(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        parent = _task(manager, sample_project, title="Parent")
        child_a = _task(manager, sample_project, title="Child A", parent_task_id=parent.id)
        _task(manager, sample_project, title="Child B", parent_task_id=parent.id)
        manager.close_task(child_a.id, force=True)

        assert task_tree_complete(manager, parent.id) is False

    def test_nested_subtree(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        parent = _task(manager, sample_project, title="Parent")
        child = _task(manager, sample_project, title="Child", parent_task_id=parent.id)
        grandchild = _task(manager, sample_project, title="Grandchild", parent_task_id=child.id)
        manager.close_task(grandchild.id, force=True)

        assert task_tree_complete(manager, parent.id) is True

    def test_nested_subtree_incomplete(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        parent = _task(manager, sample_project, title="Parent")
        child = _task(manager, sample_project, title="Child", parent_task_id=parent.id)
        _task(manager, sample_project, title="Grandchild", parent_task_id=child.id)

        assert task_tree_complete(manager, parent.id) is False


class TestTaskNeedsHumanReview:
    def test_int_task_id_escalated(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        manager.escalate_task(task.id, reason="needs human")

        assert task_needs_human_review(manager, task.seq_num) is True

    def test_int_task_id_not_escalated(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)

        assert task_needs_human_review(manager, task.seq_num) is False

    def test_needs_review_is_not_human_review(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        _start_development_stage(manager, task.id)
        manager.submit_for_review(task.id)

        assert task_needs_human_review(manager, task.id) is False

    def test_string_task_id(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project)
        manager.escalate_task(task.id, reason="needs human")

        assert task_needs_human_review(manager, task.id) is True

    def test_none_returns_false(self, temp_db) -> None:
        assert task_needs_human_review(_manager(temp_db), None) is False

    def test_no_manager_returns_false(self) -> None:
        assert task_needs_human_review(None, "#100") is False


class TestTaskTypeIn:
    def test_matches_epic_by_uuid(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="epic")

        assert task_type_in(manager, task.id, "epic") is True

    def test_matches_epic_by_uuid_instance(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="epic")

        assert task_type_in(manager, UUID(task.id), "epic") is True

    def test_matches_epic_by_hash_ref(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="epic")

        assert task_type_in(manager, _seq_ref(task), "epic") is True

    def test_matches_epic_by_int_seq_ref(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="epic")

        assert task_type_in(manager, task.seq_num, "epic") is True

    def test_matches_mixed_list_when_any_task_type_matches(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        normal = _task(manager, sample_project, title="Normal", task_type="task")
        epic = _task(manager, sample_project, title="Epic", task_type="epic")

        assert task_type_in(manager, [normal.id, epic.seq_num], "epic") is True

    def test_returns_false_for_bytes_ref_without_iterating_bytes(self, temp_db) -> None:
        assert task_type_in(_manager(temp_db), b"#1", "epic") is False

    def test_returns_false_for_missing_task(self, temp_db) -> None:
        assert task_type_in(_manager(temp_db), "#999999", "epic") is False

    def test_returns_false_without_task_manager(self) -> None:
        assert task_type_in(None, "#1", "epic") is False

    def test_returns_false_for_non_matching_type(self, temp_db, sample_project) -> None:
        manager = _manager(temp_db)
        task = _task(manager, sample_project, task_type="task")

        assert task_type_in(manager, task.id, "epic") is False
