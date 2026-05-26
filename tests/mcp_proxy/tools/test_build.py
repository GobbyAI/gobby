"""Unit tests for the build_task MCP tool contract."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _registry(temp_db: Any) -> Any:
    return create_task_ops_registry(
        LocalTaskManager(temp_db),
        sync_manager=MagicMock(),
        config=MagicMock(),
    )


def test_build_task_tool_is_registered_with_json_schema(temp_db: Any) -> None:
    registry = _registry(temp_db)

    tool = next(item for item in registry.list_tools() if item["name"] == "build_task")
    schema = tool["inputSchema"]

    assert schema["type"] == "object"
    assert schema["required"] == ["input_ref"]
    assert schema["properties"]["input_ref"]["type"] == "string"
    assert schema["properties"]["quick"]["type"] == "boolean"
    assert set(schema["properties"]["isolation"]["enum"]) == {"none", "worktree", "clone"}
    assert set(schema["properties"]["workspace_backend"]["enum"]) == {"worktree", "clone"}
    assert "default" not in schema["properties"]["workspace_backend"]
    assert schema["properties"]["clone"]["type"] == "boolean"
    assert schema["properties"]["skip_stages"]["items"]["type"] == "string"
    assert schema["properties"]["no_merge"]["type"] == "boolean"
    assert schema["properties"]["pr"]["type"] == "string"
    assert "max_review_rounds" not in schema["properties"]
    assert "max_qa_rounds" not in schema["properties"]
    assert schema["properties"]["stage"]["type"] == "array"
    assert schema["properties"]["stage"]["items"]["type"] == "string"
    assert schema["properties"]["target_branch"]["type"] == "string"
    assert schema["properties"]["agent"]["type"] == "string"
    assert schema["properties"]["reset_expansion_output"]["type"] == "boolean"
    assert schema["properties"]["max_active_agents"]["minimum"] == 1
    assert schema["properties"]["max_retries"]["minimum"] == 0
    assert schema["properties"]["coordinator"]["type"] == "string"


def test_removed_fields_are_not_exposed(temp_db: Any) -> None:
    registry = _registry(temp_db)

    tool = next(item for item in registry.list_tools() if item["name"] == "build_task")
    schema = tool["inputSchema"]

    assert {
        "profile",
        "stages",
        "add_stages",
        "unattended",
        "yolo",
        "composer_yolo",
    }.isdisjoint(schema["properties"])


@pytest.mark.asyncio
async def test_build_task_tool_calls_shared_service_and_returns_result_dict(
    temp_db: Any,
) -> None:
    from gobby.build.service import BuildResult, DispatcherTickSummary

    registry = _registry(temp_db)
    build_task = registry.get_tool("build_task")
    build_result = BuildResult(
        task_id="task-1",
        created=False,
        initial_lifecycle="in_development",
        applied_stages_skipped=["qa"],
        tick_dispatched=1,
        dispatcher_tick=DispatcherTickSummary(ticks=1, scanned=3, executed=1, skipped=0),
    )

    with patch(
        "gobby.mcp_proxy.tools.build.build", new=AsyncMock(return_value=build_result)
    ) as build:
        result = await build_task(
            input_ref="#42",
            quick=True,
            skip_stages=["qa"],
            workspace_backend="clone",
            no_merge=False,
            pr="123",
            stage=["pr:max_review_rounds=2"],
            target_branch="release/0.4",
            agent="backend-developer",
            reset_expansion_output=True,
            max_active_agents=4,
            max_retries=0,
            coordinator="#6075",
            project_id="project-1",
        )

    assert result == {
        "task_id": "task-1",
        "created": False,
        "initial_lifecycle": "in_development",
        "applied_stages_skipped": ["qa"],
        "tick_dispatched": 1,
        "dispatcher_tick": {
            "ticks": 1,
            "scanned": 3,
            "executed": 1,
            "skipped": 0,
            "cap_reached": False,
            "reason": None,
        },
        "manifest": None,
        "dry_run": False,
    }
    call = build.call_args
    assert call.args[0] == "#42"
    opts = call.args[1]
    assert opts.quick is True
    assert opts.skip_stages == ["qa"]
    assert opts.isolation == "clone"
    assert opts.isolation_explicit is True
    assert opts.no_merge is False
    assert opts.pr == "123"
    assert [
        (item.stage_name, item.max_work_attempts, item.max_review_rounds)
        for item in opts.stage_caps
    ] == [("pr", None, 2)]
    assert opts.target_branch == "release/0.4"
    assert opts.assigned_agent == "backend-developer"
    assert opts.reset_expansion_output is True
    assert opts.max_active_agents == 4
    assert opts.max_retries == 0
    assert opts.coordinator_session_ref == "#6075"
    assert call.kwargs["db"] is temp_db
    assert call.kwargs["project_id"] == "project-1"
    assert "services" in call.kwargs


@pytest.mark.asyncio
async def test_build_task_tool_omitted_backend_defaults_to_worktree(temp_db: Any) -> None:
    from gobby.build.service import BuildResult, DispatcherTickSummary

    registry = _registry(temp_db)
    build_task = registry.get_tool("build_task")
    build_result = BuildResult(
        task_id="task-1",
        created=False,
        initial_lifecycle="development",
        applied_stages_skipped=[],
        tick_dispatched=0,
        dispatcher_tick=DispatcherTickSummary(),
    )

    with patch(
        "gobby.mcp_proxy.tools.build.build", new=AsyncMock(return_value=build_result)
    ) as build:
        await build_task(input_ref="#42", quick=True, project_id="project-1")

    opts = build.call_args.args[1]
    assert opts.isolation == "worktree"
    assert opts.isolation_explicit is False


@pytest.mark.parametrize("isolation", ["none", "worktree", "clone"])
@pytest.mark.asyncio
async def test_build_task_tool_accepts_explicit_isolation(temp_db: Any, isolation: str) -> None:
    from gobby.build.service import BuildResult, DispatcherTickSummary

    registry = _registry(temp_db)
    build_task = registry.get_tool("build_task")
    build_result = BuildResult(
        task_id="task-1",
        created=False,
        initial_lifecycle="development",
        applied_stages_skipped=[],
        tick_dispatched=0,
        dispatcher_tick=DispatcherTickSummary(),
    )

    with patch(
        "gobby.mcp_proxy.tools.build.build", new=AsyncMock(return_value=build_result)
    ) as build:
        await build_task(input_ref="#42", isolation=isolation, project_id="project-1")

    opts = build.call_args.args[1]
    assert opts.isolation == isolation
    assert opts.isolation_explicit is True


@pytest.mark.parametrize("isolation", ["none", "worktree"])
@pytest.mark.asyncio
async def test_build_task_tool_rejects_clone_isolation_conflicts(
    temp_db: Any, isolation: str
) -> None:
    registry = _registry(temp_db)
    build_task = registry.get_tool("build_task")

    with pytest.raises(ValueError, match=f"clone=true conflicts with isolation={isolation}"):
        await build_task(input_ref="#42", clone=True, isolation=isolation, project_id="project-1")


@pytest.mark.asyncio
async def test_build_task_tool_clone_flag_requires_clone_backend(temp_db: Any) -> None:
    from gobby.build.service import BuildResult, DispatcherTickSummary

    registry = _registry(temp_db)
    build_task = registry.get_tool("build_task")
    build_result = BuildResult(
        task_id="task-1",
        created=False,
        initial_lifecycle="development",
        applied_stages_skipped=[],
        tick_dispatched=0,
        dispatcher_tick=DispatcherTickSummary(),
    )

    with patch(
        "gobby.mcp_proxy.tools.build.build", new=AsyncMock(return_value=build_result)
    ) as build:
        await build_task(
            input_ref="#42",
            clone=True,
            workspace_backend="clone",
            project_id="project-1",
        )

    opts = build.call_args.args[1]
    assert opts.isolation == "clone"
    assert opts.isolation_explicit is True


@pytest.mark.asyncio
async def test_build_task_tool_rejects_workspace_backend_isolation_conflict(
    temp_db: Any,
) -> None:
    registry = _registry(temp_db)
    build_task = registry.get_tool("build_task")

    with pytest.raises(ValueError, match="isolation conflicts with workspace_backend"):
        await build_task(
            input_ref="#42",
            isolation="worktree",
            workspace_backend="clone",
            project_id="project-1",
        )


@pytest.mark.asyncio
async def test_build_task_surfaces_disabled_dispatcher_cron(temp_db: Any) -> None:
    from gobby.build.service import BuildResult, DispatcherTickSummary

    registry = _registry(temp_db)
    build_task = registry.get_tool("build_task")
    build_result = BuildResult(
        task_id="task-1",
        created=False,
        initial_lifecycle="development",
        applied_stages_skipped=[],
        tick_dispatched=0,
        dispatcher_tick=DispatcherTickSummary(reason="dispatcher_cron_disabled"),
    )

    with patch("gobby.mcp_proxy.tools.build.build", new=AsyncMock(return_value=build_result)):
        result = await build_task(input_ref="#42", project_id="project-1")

    assert result["dispatcher_cron_disabled"] is True
    assert result["message"] == (
        "dispatcher_cron_disabled: dispatcher cron is disabled. "
        "Run `gobby build resume` to re-enable build automation."
    )
