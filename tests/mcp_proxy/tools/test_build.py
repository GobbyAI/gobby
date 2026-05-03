"""Unit tests for the build_task MCP tool contract."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _registry(temp_db) -> InternalToolRegistry:
    return create_task_ops_registry(
        LocalTaskManager(temp_db),
        sync_manager=MagicMock(),
        config=MagicMock(),
    )


def test_build_task_tool_is_registered_with_json_schema(temp_db) -> None:
    registry = _registry(temp_db)

    tool = next(item for item in registry.list_tools() if item["name"] == "build_task")
    schema = tool["inputSchema"]

    assert schema["type"] == "object"
    assert schema["required"] == ["input_ref"]
    assert schema["properties"]["input_ref"]["type"] == "string"
    assert set(schema["properties"]["profile"]["enum"]) == {
        "quick",
        "review",
        "full",
        "default_unattended",
        "full-unattended",
        "default_yolo",
        "full-yolo",
        "auto",
    }
    assert set(schema["properties"]["isolation"]["enum"]) == {"none", "worktree", "clone"}
    assert schema["properties"]["skip_stages"]["items"]["type"] == "string"
    assert schema["properties"]["unattended"]["type"] == "boolean"
    assert schema["properties"]["composer_yolo"]["type"] == "boolean"
    assert schema["properties"]["yolo"]["type"] == "boolean"
    assert schema["properties"]["yolo"]["deprecated"] is True
    assert "max_review_rounds" not in schema["properties"]
    assert "max_qa_rounds" not in schema["properties"]
    assert schema["properties"]["stage_caps"]["type"] == "array"
    assert (
        schema["properties"]["stage_caps"]["items"]["properties"]["stage_name"]["type"] == "string"
    )
    assert schema["properties"]["target_branch"]["type"] == "string"
    assert schema["properties"]["agent"]["type"] == "string"
    assert schema["properties"]["reset_expansion_output"]["type"] == "boolean"


def test_unattended_with_composer_yolo_distinct_fields(temp_db) -> None:
    registry = _registry(temp_db)

    tool = next(item for item in registry.list_tools() if item["name"] == "build_task")
    schema = tool["inputSchema"]

    assert schema["properties"]["unattended"]["type"] == "boolean"
    assert schema["properties"]["composer_yolo"]["type"] == "boolean"
    assert schema["properties"]["yolo"]["type"] == "boolean"
    assert schema["properties"]["yolo"]["deprecated"] is True


@pytest.mark.asyncio
async def test_build_task_tool_calls_shared_service_and_returns_result_dict(temp_db) -> None:
    from gobby.build.service import BuildResult

    registry = _registry(temp_db)
    build_task = registry.get_tool("build_task")
    build_result = BuildResult(
        task_id="task-1",
        created=False,
        initial_lifecycle="in_development",
        applied_stages_skipped=["qa"],
        tick_dispatched=1,
    )

    with patch(
        "gobby.mcp_proxy.tools.build.build", new=AsyncMock(return_value=build_result)
    ) as build:
        result = await build_task(
            input_ref="#42",
            profile="quick",
            skip_stages=["qa"],
            isolation="none",
            unattended=True,
            composer_yolo=False,
            stage_caps=[{"stage_name": "pr", "max_review_rounds": 2}],
            target_branch="release/0.4",
            agent="backend-developer",
            reset_expansion_output=True,
            project_id="project-1",
        )

    assert result == {
        "task_id": "task-1",
        "created": False,
        "initial_lifecycle": "in_development",
        "applied_stages_skipped": ["qa"],
        "tick_dispatched": 1,
        "manifest": None,
    }
    call = build.call_args
    assert call.args[0] == "#42"
    opts = call.args[1]
    assert opts.profile == "quick"
    assert opts.skip_stages == ["qa"]
    assert opts.isolation == "none"
    assert opts.unattended is True
    assert opts.composer_yolo is False
    assert [
        (item.stage_name, item.max_work_attempts, item.max_review_rounds)
        for item in opts.stage_caps
    ] == [("pr", None, 2)]
    assert opts.target_branch == "release/0.4"
    assert opts.assigned_agent == "backend-developer"
    assert opts.reset_expansion_output is True
    assert call.kwargs["db"] is temp_db
    assert call.kwargs["project_id"] == "project-1"
