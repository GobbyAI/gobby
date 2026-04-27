"""Tests for task_artifacts plan_file_hash and MCP schema exposure."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.tasks import LocalTaskManager, TaskArtifactManager

pytestmark = pytest.mark.unit


def test_set_artifact_plan_hash_round_trips(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Plan hash",
    )
    manager = TaskArtifactManager(temp_db)

    manager.set_artifact(task.id, "plan_file_hash", "sha256:abc")
    artifacts = manager.get_artifacts(task.id)

    assert artifacts.plan_file_hash == "sha256:abc"


def test_mcp_get_artifacts_includes_plan_file_hash(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
    registry = create_task_ops_registry(task_manager, sync_manager=MagicMock())
    task = task_manager.create_task(project_id=sample_project["id"], title="Plan hash")
    set_artifact = registry.get_tool("set_artifact")
    get_artifacts = registry.get_tool("get_artifacts")

    result = set_artifact(task_id=task.id, field="plan_file_hash", value="sha256:def")
    artifacts = get_artifacts(task_id=task.id)

    assert result["ok"] is True
    assert artifacts["plan_file_hash"] == "sha256:def"
    for tool_name in (
        "get_artifacts",
        "set_artifact",
        "set_artifacts_atomic",
        "clear_isolation_pair",
    ):
        schema = registry.get_schema(tool_name)
        assert schema is not None
        artifact_fields = schema["inputSchema"]["x-artifact-fields"]
        assert artifact_fields["base_commit_sha"]["type"] == ["string", "null"]
        assert artifact_fields["plan_file_hash"]["type"] == ["string", "null"]
