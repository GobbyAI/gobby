"""Exact any-of label filtering through MCP and the PostgreSQL task manager."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.services.argument_validation import check_arguments
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("labels", "label", "expected"),
    [
        (["covers:plan:1.1:1", "covers:plan:1.2:1"], None, ["first", "second"]),
        (["covers:plan:1.1:1"], None, ["first"]),
        ([], None, []),
        (["covers:plan:1.1:1", "covers:plan:1.2:1"], "extra", ["second"]),
        (None, "covers:plan:1.1:1", ["first"]),
    ],
)
async def test_exact_label_filters_and_parent_open_count(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    labels: list[str] | None,
    label: str | None,
    expected: list[str],
) -> None:
    manager = LocalTaskManager(temp_db)
    project_id = sample_project["id"]
    parent = manager.create_task(project_id, "Parent", task_type="epic")
    tasks = {}
    for name, task_labels in (
        ("first", ["covers:plan:1.1:1"]),
        ("second", ["covers:plan:1.2:1", "extra"]),
        ("prefix-neighbor", ["covers:plan:1.1:10"]),
        ("unrelated", []),
    ):
        tasks[name] = manager.create_task(
            project_id,
            name,
            labels=task_labels,
            parent_task_id=parent.id,
            validation_criteria="Exact label selection is observable.",
        )
    registry = create_task_registry(manager, MagicMock())
    arguments: dict[str, Any] = {
        "project": project_id,
        "parent_task_id": parent.id,
        "sort_by": "created_at",
        "limit": 1,
    }
    if labels is not None:
        arguments["labels"] = labels
    if label is not None:
        arguments["label"] = label
    schema = registry.get_schema("list_tasks")
    assert schema is not None
    assert check_arguments(arguments, schema["inputSchema"]) == []
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as context:
        context.return_value = {"id": project_id}
        first = await registry.call("list_tasks", arguments)
        second = await registry.call("list_tasks", {**arguments, "offset": 1})
    actual = [task["id"] for page in (first, second) for task in page["tasks"]]
    assert actual == [tasks[name].id for name in expected]
    assert first["open_count"] == len(expected)
    assert second["open_count"] == len(expected)
