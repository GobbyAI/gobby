from __future__ import annotations

import pytest

from gobby.storage.delivery import TaskDeliveryStateManager
from tests.storage.tasks._stage_test_helpers import create_task

pytestmark = pytest.mark.unit


def test_delivery_state_records_campaign_and_units(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    manager = TaskDeliveryStateManager(temp_db)

    manager.record_campaign(
        task.id,
        state="ready_to_merge",
        merge_strategy="squash",
        structured_pr_verdict={
            "verdict": "approve",
            "findings": {"summary": "ready"},
            "report_ref": "https://github.test/pr/1",
        },
        pr_report_ref="https://github.test/pr/1",
    )
    manager.record_unit(
        task.id,
        worktree_id="wt-1",
        repo="owner/repo",
        source_branch="task-branch",
        target_branch="main",
        pr_required=True,
        protection_json={"requires_pr": True, "requires_review_count": 1},
        pr_url="https://github.test/pr/1",
        github_pr_number=1,
        gate_snapshot_json={"state": "ready"},
        pr_state="ready_to_land",
    )

    state = manager.get_state(task.id)

    assert state["campaign"]["state"] == "ready_to_merge"
    assert state["campaign"]["structured_pr_verdict"] == {
        "verdict": "approve",
        "findings": {"summary": "ready"},
        "report_ref": "https://github.test/pr/1",
    }
    assert state["units"] == [
        {
            "id": state["units"][0]["id"],
            "task_id": task.id,
            "unit_key": "worktree:wt-1",
            "worktree_id": "wt-1",
            "repo": "owner/repo",
            "source_branch": "task-branch",
            "target_branch": "main",
            "pr_required": True,
            "protection": {"requires_pr": True, "requires_review_count": 1},
            "pr_url": "https://github.test/pr/1",
            "github_pr_number": 1,
            "gate_snapshot": {"state": "ready"},
            "pr_state": "ready_to_land",
            "local_update_attempts": 0,
            "last_error": None,
            "created_at": state["units"][0]["created_at"],
            "updated_at": state["units"][0]["updated_at"],
        }
    ]


def test_delivery_unit_upsert_preserves_one_unit_per_key(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    manager = TaskDeliveryStateManager(temp_db)

    manager.record_unit(task.id, unit_key="main", pr_required=True, pr_state="open")
    manager.record_unit(task.id, unit_key="main", pr_required=False, pr_state="direct_merge")

    state = manager.get_state(task.id)
    assert len(state["units"]) == 1
    assert state["units"][0]["pr_required"] is False
    assert state["units"][0]["pr_state"] == "direct_merge"


def test_delivery_campaign_upsert_preserves_null_updates(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    manager = TaskDeliveryStateManager(temp_db)

    manager.record_campaign(task.id, last_error="blocked")
    updated = manager.record_campaign(task.id, last_error=None)

    assert updated["last_error"] is None
    assert manager.get_state(task.id)["campaign"]["last_error"] is None


def test_delivery_json_decode_returns_none_for_malformed_json(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    manager = TaskDeliveryStateManager(temp_db)
    manager.record_campaign(task.id, structured_pr_verdict={"verdict": "approve"})
    temp_db.execute(
        "UPDATE task_delivery_campaigns SET structured_pr_verdict = ? WHERE task_id = ?",
        ("{malformed", task.id),
    )

    state = manager.get_state(task.id)

    assert state["campaign"]["structured_pr_verdict"] is None
