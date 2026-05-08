"""MCP get_task response shape drops legacy task-state fields."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_no_legacy_fields(task_registry, mock_task_manager) -> None:
    task = SimpleNamespace(
        id="task-1",
        seq_num=1,
        to_brief=lambda: {"id": "task-1", "state": {"current_stage": {"name": "dev"}}},
    )
    mock_task_manager.get_task.return_value = task

    result = task_registry.call_sync("get_task", {"task_id": "task-1"})

    assert "status" not in result
    assert "lifecycle" not in result
    assert "lifecycle_stage" not in result
