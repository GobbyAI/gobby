"""Red tests for task artifact MCP operations on gobby-tasks-ops."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _registry(temp_db) -> tuple[LocalTaskManager, object]:
    task_manager = LocalTaskManager(temp_db)
    return task_manager, create_task_ops_registry(task_manager, sync_manager=MagicMock())


def test_artifact_tools_are_registered_on_tasks_ops(temp_db) -> None:
    _task_manager, registry = _registry(temp_db)

    tool_names = {tool["name"] for tool in registry.list_tools()}

    assert {
        "set_artifact",
        "set_artifacts_atomic",
        "clear_isolation_pair",
        "append_description_section",
        "get_artifacts",
    }.issubset(tool_names)


def test_append_description_section_is_idempotent_for_same_heading_and_body(
    temp_db,
    sample_project,
) -> None:
    task_manager, registry = _registry(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Audit marker",
        description="Existing description.",
    )
    append_section = registry.get_tool("append_description_section")

    first = append_section(
        task_id=task.id,
        heading="Agent Selection",
        body="Defaulted to backend-developer because assigned_agent was unset.",
    )
    second = append_section(
        task_id=task.id,
        heading="Agent Selection",
        body="Defaulted to backend-developer because assigned_agent was unset.",
    )

    updated = task_manager.get_task(task.id)
    assert first["appended"] is True
    assert second["appended"] is False
    assert updated.description.count("## Agent Selection") == 1
    assert "Defaulted to backend-developer" in updated.description


def test_artifact_tools_mutate_and_fetch_artifacts(temp_db, sample_project) -> None:
    task_manager, registry = _registry(temp_db)
    task = task_manager.create_task(project_id=sample_project["id"], title="Artifacts")
    set_artifacts_atomic = registry.get_tool("set_artifacts_atomic")
    clear_isolation_pair = registry.get_tool("clear_isolation_pair")
    get_artifacts = registry.get_tool("get_artifacts")

    set_result = set_artifacts_atomic(
        task_id=task.id,
        fields={
            "worktree_path": "/tmp/gobby-wt",
            "worktree_id": "worktree-row-1",
            "target_branch": "release/0.4",
        },
    )

    artifacts = get_artifacts(task_id=task.id)
    assert set_result["ok"] is True
    assert artifacts["worktree_path"] == "/tmp/gobby-wt"
    assert artifacts["worktree_id"] == "worktree-row-1"
    assert artifacts["target_branch"] == "release/0.4"

    clear_result = clear_isolation_pair(task_id=task.id, family="worktree")
    artifacts = get_artifacts(task_id=task.id)
    assert clear_result["ok"] is True
    assert artifacts["worktree_path"] is None
    assert artifacts["worktree_id"] is None
    assert artifacts["target_branch"] == "release/0.4"
