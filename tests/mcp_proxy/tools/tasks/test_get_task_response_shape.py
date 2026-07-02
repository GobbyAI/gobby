"""MCP get_task response shape drops legacy task-state fields."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.mcp_proxy.tools.tasks._formatters import task_discovery_payload, task_summary_payload

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


def test_task_discovery_ref_uses_seq_num_from_dict() -> None:
    task = SimpleNamespace(
        id="03940009-0faa-4d80-9b96-289df7f44431",
        to_dict=lambda: {
            "id": "03940009-0faa-4d80-9b96-289df7f44431",
            "seq_num": 17424,
            "title": "Apply review fixes",
        },
    )

    payload = task_discovery_payload(task)

    assert payload["seq_num"] == 17424
    assert payload["ref"] == "#17424"


def test_task_summary_uses_to_dict_payload_fields() -> None:
    task = SimpleNamespace(
        to_dict=lambda: {
            "id": "03940009-0faa-4d80-9b96-289df7f44431",
            "seq_num": 17425,
            "title": "Apply summary fixes",
            "task_type": "bug",
            "category": "code",
            "priority": 1,
            "validation_criteria": "Summary payload uses to_dict fields.",
            "allow_automation": True,
        },
    )

    payload = task_summary_payload(task, dependencies={})

    assert payload["ref"] == "#17425"
    assert payload["id"] == "03940009-0faa-4d80-9b96-289df7f44431"
    assert payload["title"] == "Apply summary fixes"
    assert payload["validation_criteria"] == "Summary payload uses to_dict fields."
    assert payload["allow_automation"] is True
    assert payload["state"]["is_closed"] is False


def test_task_summary_preserves_attribute_fields_missing_from_to_dict() -> None:
    task = SimpleNamespace(
        id="03940009-0faa-4d80-9b96-289df7f44431",
        seq_num=17426,
        title="Preserve attribute fields",
        task_type="bug",
        category="code",
        priority=1,
        closed_at="2026-07-02T00:00:00Z",
        current_stage={"name": "done", "state": "done"},
        validation_criteria="Attribute-backed fields survive formatting.",
        allow_automation=True,
        to_dict=lambda: {
            "id": "03940009-0faa-4d80-9b96-289df7f44431",
            "seq_num": 17426,
            "title": "Preserve attribute fields",
        },
    )

    payload = task_summary_payload(task, dependencies={})

    assert payload["task_type"] == "bug"
    assert payload["category"] == "code"
    assert payload["validation_criteria"] == "Attribute-backed fields survive formatting."
    assert payload["allow_automation"] is True
    assert payload["state"]["is_closed"] is True
    assert payload["state"]["closed_at"] == "2026-07-02T00:00:00Z"
    assert payload["state"]["current_stage"] == {"name": "done", "state": "done"}
