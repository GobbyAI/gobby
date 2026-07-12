"""Task stage tools are discoverable only on their intended MCP server."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.mcp_proxy.tools.tasks._stage_registry_ops import create_stage_registry_ops_registry
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._stage_registry import StageRegistryEntry

pytestmark = pytest.mark.unit

READ_STAGE_TOOLS = {"get_task_stages", "list_stages_registry", "get_task_type_defaults"}
MUTATING_STAGE_TOOLS = {
    "update_stage",
    "restore_stage",
    "delete_stage",
    "set_task_type_defaults",
}


def _tool_names(registry: Any) -> set[str]:
    return {tool["name"] for tool in registry.list_tools()}


def test_mutating_tools_absent_from_gobby_tasks(
    mock_task_manager: Any,
    mock_sync_manager: Any,
) -> None:
    names = _tool_names(create_task_registry(mock_task_manager, mock_sync_manager))

    assert names.isdisjoint(MUTATING_STAGE_TOOLS)


def test_mutating_tools_visible_on_gobby_tasks_ops(
    mock_task_manager: Any,
    mock_sync_manager: Any,
) -> None:
    names = _tool_names(create_task_ops_registry(mock_task_manager, mock_sync_manager))

    assert MUTATING_STAGE_TOOLS <= names


def test_read_tools_absent_from_gobby_tasks_ops(
    mock_task_manager: Any,
    mock_sync_manager: Any,
) -> None:
    names = _tool_names(create_task_ops_registry(mock_task_manager, mock_sync_manager))

    assert names.isdisjoint(READ_STAGE_TOOLS)


def test_read_tools_visible_on_gobby_tasks(
    mock_task_manager: Any,
    mock_sync_manager: Any,
) -> None:
    names = _tool_names(create_task_registry(mock_task_manager, mock_sync_manager))

    assert READ_STAGE_TOOLS <= names


def _registry_with_custom_stage(temp_db: Any) -> Any:
    manager = LocalTaskManager(temp_db)
    manager.stages_registry.upsert(
        StageRegistryEntry(
            name="operator_review",
            display_label="Operator Review",
            description="A local operator review gate.",
            category="verification",
            default_agent=None,
            reviewer_agent="qa-reviewer",
            reviewer_agent_selector_json=None,
            review_policy="optional",
            dispatch_type="pipeline",
            dispatch_target="operator-review",
            dispatch_inputs_json=None,
            position_hint=999,
            requires_human=True,
            is_terminal=False,
            default_max_work_attempts=2,
            default_max_review_rounds=7,
        )
    )
    return create_stage_registry_ops_registry(
        RegistryContext(task_manager=manager, sync_manager=MagicMock())
    )


@pytest.mark.asyncio
async def test_update_stage_rejects_unknown_keys_with_structured_error(temp_db: Any) -> None:
    registry = _registry_with_custom_stage(temp_db)

    result = await registry.call(
        "update_stage",
        {"name": "operator_review", "updates": {"display_lable": "Misspelled"}},
    )

    assert result == {
        "ok": False,
        "error": "invalid_stage_update",
        "message": "Unknown stage update field(s): display_lable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["update_stage", "restore_stage", "delete_stage"])
async def test_stage_registry_mutations_return_not_found_error(
    temp_db: Any,
    tool_name: str,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_stage_registry_ops_registry(
        RegistryContext(task_manager=manager, sync_manager=MagicMock())
    )
    arguments: dict[str, Any] = {"name": "missing_stage"}
    if tool_name == "update_stage":
        arguments["updates"] = {"description": "Still missing"}

    result = await registry.call(tool_name, arguments)

    assert result["ok"] is False
    assert result["error"] == "stage_not_found"
    assert "missing_stage" in result["message"]
