from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.tasks import LocalTaskManager
from tests.storage.tasks._stage_test_helpers import create_task

pytestmark = pytest.mark.unit


def _registry(temp_db):
    return create_task_ops_registry(
        LocalTaskManager(temp_db),
        sync_manager=MagicMock(),
    )


def test_delivery_state_tools_are_registered(temp_db) -> None:
    registry = _registry(temp_db)

    assert registry.get_tool("record_pr_state") is not None
    assert registry.get_tool("get_delivery_state") is not None


def test_record_pr_state_persists_delivery_unit(temp_db, sample_project) -> None:
    task = create_task(temp_db, sample_project, task_type="feature")
    registry = _registry(temp_db)
    record = registry.get_tool("record_pr_state")
    get_state = registry.get_tool("get_delivery_state")
    assert record is not None
    assert get_state is not None

    result = record(
        task_id=task.id,
        worktree_id="wt-1",
        repo="owner/repo",
        source_branch="feature/task",
        target_branch="main",
        pr_required=True,
        protection={"requires_pr": True},
        pr_state="awaiting_ci",
        campaign_state="pr_open",
        merge_strategy="squash",
    )

    assert result["delivery"]["campaign"]["state"] == "pr_open"
    state = get_state(task_id=task.id)["delivery"]
    assert state["campaign"]["merge_strategy"] == "squash"
    assert state["units"][0]["unit_key"] == "worktree:wt-1"
    assert state["units"][0]["protection"] == {"requires_pr": True}
    assert state["units"][0]["pr_state"] == "awaiting_ci"
