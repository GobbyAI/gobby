"""Tests for task_claim_state helpers."""

from typing import Any

import pytest

from gobby.workflows import task_claim_state
from gobby.workflows.task_claim_state import (
    active_task_id_for_edit,
    add_claimed_task,
    remove_claimed_task,
    target_task_has_edits,
    task_edited_file_times,
)

pytestmark = pytest.mark.unit


class TestAddClaimedTask:
    def test_adds_to_empty(self) -> None:
        variables: dict = {}
        result = add_claimed_task(variables, "uuid-1", "#1")
        assert result == {
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#1"},
            "active_task_id": "uuid-1",
        }

    def test_adds_second_task(self) -> None:
        variables = {"claimed_tasks": {"uuid-1": "#1"}}
        result = add_claimed_task(variables, "uuid-2", "#2")
        assert result["task_claimed"] is True
        assert result["claimed_tasks"] == {"uuid-1": "#1", "uuid-2": "#2"}
        assert result["active_task_id"] == "uuid-2"

    def test_idempotent_on_duplicate(self) -> None:
        variables = {"claimed_tasks": {"uuid-1": "#1"}}
        result = add_claimed_task(variables, "uuid-1", "#1")
        assert result["claimed_tasks"] == {"uuid-1": "#1"}
        assert result["task_claimed"] is True
        assert result["active_task_id"] == "uuid-1"

    def test_does_not_mutate_original(self) -> None:
        original = {"uuid-1": "#1"}
        variables = {"claimed_tasks": original}
        result = add_claimed_task(variables, "uuid-2", "#2")
        assert "uuid-2" not in original
        assert "uuid-2" in result["claimed_tasks"]

    def test_handles_none_claimed_tasks(self) -> None:
        variables = {"claimed_tasks": None}
        result = add_claimed_task(variables, "uuid-1", "#1")
        assert result == {
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#1"},
            "active_task_id": "uuid-1",
        }


class TestRemoveClaimedTask:
    def test_removes_one_of_two(self) -> None:
        variables = {
            "active_task_id": "uuid-1",
            "claimed_tasks": {"uuid-1": "#1", "uuid-2": "#2"},
            "task_has_commits": True,
            "task_edited_files": {"uuid-1": ["a.py"], "uuid-2": ["b.py"]},
        }
        result = remove_claimed_task(variables, "uuid-1")
        assert result["task_claimed"] is True
        assert result["claimed_tasks"] == {"uuid-2": "#2"}
        assert result["active_task_id"] == "uuid-2"
        assert (variables | result)["task_has_commits"] is True
        assert result["task_edited_files"] == {"uuid-2": ["b.py"]}

    def test_removes_last_sets_false(self) -> None:
        variables = {
            "active_task_id": "uuid-1",
            "claimed_tasks": {"uuid-1": "#1"},
            "task_has_commits": True,
            "task_edited_files": {"uuid-1": ["a.py"]},
        }
        result = remove_claimed_task(variables, "uuid-1")
        assert result["task_claimed"] is False
        assert result["claimed_tasks"] == {}
        assert result["active_task_id"] is None
        assert (variables | result)["task_has_commits"] is False
        assert result["task_edited_files"] == {}

    def test_noop_on_missing_task_id(self) -> None:
        variables = {"active_task_id": "uuid-1", "claimed_tasks": {"uuid-1": "#1"}}
        result = remove_claimed_task(variables, "uuid-999")
        assert result["task_claimed"] is True
        assert result["claimed_tasks"] == {"uuid-1": "#1"}
        assert result["active_task_id"] == "uuid-1"

    def test_removes_from_empty(self) -> None:
        variables: dict = {}
        result = remove_claimed_task(variables, "uuid-1")
        assert result["task_claimed"] is False
        assert result["claimed_tasks"] == {}
        assert result["active_task_id"] is None
        assert result["task_edited_files"] == {}

    def test_does_not_mutate_original(self) -> None:
        original = {"uuid-1": "#1", "uuid-2": "#2"}
        variables = {"claimed_tasks": original}
        result = remove_claimed_task(variables, "uuid-1")
        assert "uuid-1" in original  # Original unchanged
        assert "uuid-1" not in result["claimed_tasks"]

    def test_remove_claimed_task_still_discards_edit_attribution(self) -> None:
        """B.2: closing a task drops its attribution, leaving no stale entry behind."""
        variables = {
            "active_task_id": "uuid-1",
            "claimed_tasks": {"uuid-1": "#1"},
            "task_edited_files": {"uuid-1": ["a.py"]},
            "task_edited_file_checkouts": {"uuid-1": {"/repo": ["a.py"]}},
            "task_edited_file_times": {"uuid-1": {"a.py": 100.0}, "uuid-2": {"b.py": 200.0}},
        }

        result = remove_claimed_task(variables, "uuid-1")

        assert result["task_edited_files"] == {}
        assert result["task_edited_file_checkouts"] == {}
        assert result["task_edited_file_times"] == {"uuid-2": {"b.py": 200.0}}


class TestTaskEditedFileTimes:
    def test_reads_numeric_stamps_by_normalized_path(self) -> None:
        variables = {
            "task_edited_file_times": {
                "uuid-1": {
                    "src/./a.py": 100,
                    "src/b.py": 200.5,
                    "src/flag.py": True,
                    "src/text.py": "300",
                    "/abs.py": 400.0,
                },
            }
        }

        assert task_edited_file_times(variables, "uuid-1") == {
            "src/a.py": 100.0,
            "src/b.py": 200.5,
        }

    @pytest.mark.parametrize(
        "variables",
        [
            {},
            {"task_edited_file_times": "junk"},
            {"task_edited_file_times": {"uuid-1": ["a.py"]}},
            {"task_edited_file_times": {"uuid-2": {"a.py": 1.0}}},
        ],
    )
    def test_missing_or_malformed_ledger_is_empty(self, variables: dict[str, Any]) -> None:
        assert task_edited_file_times(variables, "uuid-1") == {}


class TestReleaseClaimedTask:
    def test_release_claimed_task_keeps_edit_attribution(self) -> None:
        """B.1: escalation is a pause, so the task keeps the files it edited.

        The claim itself is released exactly the way `remove_claimed_task` releases
        it, because the two differ only in what they do with the attribution.
        """
        variables: dict[str, Any] = {
            "active_task_id": "uuid-1",
            "claimed_tasks": {"uuid-1": "#1", "uuid-2": "#2"},
            "task_has_commits": True,
            "task_edited_files": {"uuid-1": ["a.py"], "uuid-2": ["b.py"]},
            "task_edited_file_checkouts": {"uuid-1": {"/repo": ["a.py"]}},
        }

        released = task_claim_state.release_claimed_task(variables, "uuid-1")
        removed = remove_claimed_task(dict(variables), "uuid-1")

        merged = variables | released
        assert released["claimed_tasks"] == removed["claimed_tasks"] == {"uuid-2": "#2"}
        assert released["active_task_id"] == removed["active_task_id"] == "uuid-2"
        assert released["task_claimed"] == removed["task_claimed"] is True
        assert merged["task_edited_files"] == {"uuid-1": ["a.py"], "uuid-2": ["b.py"]}
        assert merged["task_edited_file_checkouts"] == {"uuid-1": {"/repo": ["a.py"]}}

    def test_releasing_the_last_claim_clears_the_claim_flags_only(self) -> None:
        """The session stops holding a claim, yet still owns the files it edited."""
        variables: dict[str, Any] = {
            "active_task_id": "uuid-1",
            "claimed_tasks": {"uuid-1": "#1"},
            "task_has_commits": True,
            "task_edited_files": {"uuid-1": ["a.py"]},
        }

        result = task_claim_state.release_claimed_task(variables, "uuid-1")

        assert result["task_claimed"] is False
        assert result["claimed_tasks"] == {}
        assert result["active_task_id"] is None
        assert (variables | result)["task_has_commits"] is False
        assert (variables | result)["task_edited_files"] == {"uuid-1": ["a.py"]}

    def test_does_not_mutate_original(self) -> None:
        original = {"uuid-1": "#1", "uuid-2": "#2"}
        variables = {"claimed_tasks": original}

        result = task_claim_state.release_claimed_task(variables, "uuid-1")

        assert "uuid-1" in original
        assert "uuid-1" not in result["claimed_tasks"]


class TestActiveTaskIdForEdit:
    def test_uses_active_task_when_multiple_claimed(self) -> None:
        variables = {
            "active_task_id": "uuid-2",
            "claimed_tasks": {"uuid-1": "#1", "uuid-2": "#2"},
        }

        assert active_task_id_for_edit(variables) == "uuid-2"

    def test_sole_claim_fallback(self) -> None:
        variables = {"claimed_tasks": {"uuid-1": "#1"}}

        assert active_task_id_for_edit(variables) == "uuid-1"

    def test_multiple_claims_without_active_does_not_guess(self) -> None:
        variables = {"claimed_tasks": {"uuid-1": "#1", "uuid-2": "#2"}}

        assert active_task_id_for_edit(variables) is None

    def test_no_claim_records_no_task(self) -> None:
        assert active_task_id_for_edit({}) is None


class TestTargetTaskHasEdits:
    def test_missing_task_id_short_circuits(self) -> None:
        assert target_task_has_edits({"task_edited_files": {"task-1": []}}, None) is False

    def test_missing_task_key_means_no_mutation_observed(self) -> None:
        assert target_task_has_edits({"task_edited_files": {}}, "task-1") is False

    def test_non_list_task_entry_means_no_mutation_observed(self) -> None:
        assert (
            target_task_has_edits({"task_edited_files": {"task-1": "file.py"}}, "task-1") is False
        )

    def test_empty_task_entry_means_no_mutation_observed(self) -> None:
        variables: dict[str, Any] = {"task_edited_files": {"task-1": []}}

        assert target_task_has_edits(variables, "task-1") is False
