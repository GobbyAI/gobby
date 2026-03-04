"""Tests for task_claim_state helpers."""

import pytest

from gobby.workflows.task_claim_state import add_claimed_task, remove_claimed_task

pytestmark = pytest.mark.unit


class TestAddClaimedTask:
    def test_adds_to_empty(self) -> None:
        variables: dict = {}
        result = add_claimed_task(variables, "uuid-1", "#1")
        assert result == {"task_claimed": True, "claimed_tasks": {"uuid-1": "#1"}}

    def test_adds_second_task(self) -> None:
        variables = {"claimed_tasks": {"uuid-1": "#1"}}
        result = add_claimed_task(variables, "uuid-2", "#2")
        assert result["task_claimed"] is True
        assert result["claimed_tasks"] == {"uuid-1": "#1", "uuid-2": "#2"}

    def test_idempotent_on_duplicate(self) -> None:
        variables = {"claimed_tasks": {"uuid-1": "#1"}}
        result = add_claimed_task(variables, "uuid-1", "#1")
        assert result["claimed_tasks"] == {"uuid-1": "#1"}
        assert result["task_claimed"] is True

    def test_does_not_mutate_original(self) -> None:
        original = {"uuid-1": "#1"}
        variables = {"claimed_tasks": original}
        result = add_claimed_task(variables, "uuid-2", "#2")
        assert "uuid-2" not in original
        assert "uuid-2" in result["claimed_tasks"]

    def test_handles_none_claimed_tasks(self) -> None:
        variables = {"claimed_tasks": None}
        result = add_claimed_task(variables, "uuid-1", "#1")
        assert result == {"task_claimed": True, "claimed_tasks": {"uuid-1": "#1"}}


class TestRemoveClaimedTask:
    def test_removes_one_of_two(self) -> None:
        variables = {"claimed_tasks": {"uuid-1": "#1", "uuid-2": "#2"}}
        result = remove_claimed_task(variables, "uuid-1")
        assert result["task_claimed"] is True
        assert result["claimed_tasks"] == {"uuid-2": "#2"}

    def test_removes_last_sets_false(self) -> None:
        variables = {"claimed_tasks": {"uuid-1": "#1"}}
        result = remove_claimed_task(variables, "uuid-1")
        assert result["task_claimed"] is False
        assert result["claimed_tasks"] == {}

    def test_noop_on_missing_task_id(self) -> None:
        variables = {"claimed_tasks": {"uuid-1": "#1"}}
        result = remove_claimed_task(variables, "uuid-999")
        assert result["task_claimed"] is True
        assert result["claimed_tasks"] == {"uuid-1": "#1"}

    def test_removes_from_empty(self) -> None:
        variables: dict = {}
        result = remove_claimed_task(variables, "uuid-1")
        assert result["task_claimed"] is False
        assert result["claimed_tasks"] == {}

    def test_does_not_mutate_original(self) -> None:
        original = {"uuid-1": "#1", "uuid-2": "#2"}
        variables = {"claimed_tasks": original}
        result = remove_claimed_task(variables, "uuid-1")
        assert "uuid-1" in original  # Original unchanged
        assert "uuid-1" not in result["claimed_tasks"]
