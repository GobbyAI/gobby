"""Regression coverage for MCP task-list filters and counts."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_candidate_filter_uses_sent_event_and_deduplicates_review_recipients(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    seat = session_manager.register(
        external_id="candidate-author",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    candidate = manager.create_task(
        sample_project["id"],
        "Waiting for landing",
        validation_criteria="Explicit candidate event appears once in backlog.",
    )
    linked = manager.create_task(
        sample_project["id"],
        "Commit linked, still implementing",
        validation_criteria="Linked commits alone do not enter the candidate backlog.",
    )
    manager.link_commit(linked.id, "a" * 40)
    messages = InterSessionMessageManager(temp_db)
    event = f"LANE=1 EVENT=CANDIDATE TASK=#{candidate.seq_num} TASK_TITLE=Waiting RUN=run"
    first = messages.create_message(seat.id, seat.id, event)
    messages.create_message(seat.id, seat.id, event)
    messages.create_message(seat.id, seat.id, f"EVENT=CANDIDATE TASK=#{linked.seq_num}0 fake")
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as context:
        context.return_value = {"id": sample_project["id"]}
        result = await registry.call(
            "list_tasks",
            {
                "candidate": True,
                "closed": False,
                "view": "compact",
            },
        )
    assert result["count"] == 1
    row = dict(zip(result["columns"], result["rows"][0], strict=True))
    assert row["ref"] == f"#{candidate.seq_num}"
    assert row["candidate_sent_at"] == first.sent_at.isoformat()
    assert row["receipts"] == []


@pytest.mark.asyncio
async def test_list_tasks_resolves_ordered_refs_and_recent_escalations(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    tasks = [
        manager.create_task(
            sample_project["id"],
            f"Queue work {number}",
            validation_criteria="Queue title and escalation evidence are returned.",
        )
        for number in range(3)
    ]
    temp_db.execute(
        "UPDATE tasks SET is_escalated = TRUE, escalation_reason = %s, "
        "escalated_at = %s, updated_at = %s WHERE id = %s",
        ("Waiting on review", "2026-10-12T14:00:00Z", "2026-10-12T14:00:00Z", tasks[1].id),
    )
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as context:
        context.return_value = {"id": sample_project["id"]}
        ordered = await registry.call(
            "list_tasks",
            {
                "refs": [f"#{tasks[2].seq_num}", f"#{tasks[0].seq_num}"],
                "view": "compact",
            },
        )
        recent = await registry.call(
            "list_tasks",
            {
                "escalated": True,
                "updated_since": "2026-10-12T13:30:00Z",
                "view": "compact",
            },
        )
        older = await registry.call(
            "list_tasks",
            {
                "escalated": True,
                "updated_since": "2026-10-12T14:01:00Z",
                "view": "compact",
            },
        )
    rows = [dict(zip(ordered["columns"], row, strict=True)) for row in ordered["rows"]]
    assert [row["title"] for row in rows] == [tasks[2].title, tasks[0].title]
    assert ordered["next_offset"] is None
    recent_row = dict(zip(recent["columns"], recent["rows"][0], strict=True))
    assert recent_row["ref"] == f"#{tasks[1].seq_num}"
    assert recent_row["escalation_reason"] == "Waiting on review"
    assert recent_row["escalated_at"] == "2026-10-12T14:00:00+00:00"
    assert older["rows"] == []


@pytest.mark.asyncio
async def test_list_tasks_pages_project_label_owners_in_stable_order(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    project_id = sample_project["id"]
    owners = [
        manager.create_task(
            project_id,
            f"Owner {number}",
            labels=["covers:p:1.1:1.1.1"],
            validation_criteria="Owner is visible in stable coverage pages.",
        )
        for number in range(3)
    ]
    manager.create_task(
        project_id,
        "Unrelated",
        labels=["other"],
        validation_criteria="Unrelated task is excluded from coverage pages.",
    )
    # Equal timestamps exercise the storage ID tie-break across page boundaries.
    for owner in owners:
        temp_db.execute(
            "UPDATE tasks SET created_at = %s WHERE id = %s",
            ("2026-10-07T00:00:00+00:00", owner.id),
        )
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
        mock_ctx.return_value = {"id": project_id}
        pages = [
            await registry.call(
                "list_tasks",
                {
                    "label": "covers:p:1.1:1.1.1",
                    "limit": 2,
                    "offset": offset,
                    "sort_by": "created_at",
                },
            )
            for offset in (0, 2, 4)
        ]

    assert [page["count"] for page in pages] == [2, 1, 0]
    assert [task["id"] for page in pages for task in page["tasks"]] == sorted(
        owner.id for owner in owners
    )


@pytest.mark.asyncio
async def test_list_tasks_filters_closed_children_and_reports_open_count(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    task_manager = LocalTaskManager(temp_db)
    registry = create_task_registry(task_manager, MagicMock())
    project_id = sample_project["id"]
    criteria = "Task-list filtering is observable."
    parent = task_manager.create_task(project_id, "Parent", validation_criteria=criteria)
    open_tasks = [
        task_manager.create_task(
            project_id,
            title,
            parent_task_id=parent.id,
            validation_criteria=criteria,
        )
        for title in ("Open one", "Open two")
    ]
    closed_task = task_manager.create_task(
        project_id,
        "Closed",
        parent_task_id=parent.id,
        validation_criteria=criteria,
    )
    temp_db.execute(
        "UPDATE tasks SET closed_at = %s, closed_reason = %s WHERE id = %s",
        ("2026-09-01T00:00:00+00:00", "test-complete", closed_task.id),
    )

    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
        mock_ctx.return_value = {"id": project_id}
        open_result = await registry.call(
            "list_tasks",
            {"parent_task_id": parent.id, "closed": False, "limit": 1},
        )
        closed_result = await registry.call(
            "list_tasks",
            {"parent_task_id": parent.id, "closed": True},
        )
        all_result = await registry.call("list_tasks", {"parent_task_id": parent.id})

    assert open_result["open_count"] == 2
    assert len(open_result["tasks"]) == 1
    assert open_result["tasks"][0]["state"]["is_closed"] is False
    assert closed_result["open_count"] == 2
    assert [task["id"] for task in closed_result["tasks"]] == [closed_task.id]
    assert all(task["state"]["is_closed"] for task in closed_result["tasks"])
    assert all_result["open_count"] == 2
    assert {task["id"] for task in all_result["tasks"]} == {
        open_tasks[0].id,
        open_tasks[1].id,
        closed_task.id,
    }
    assert {task["state"]["is_closed"] for task in all_result["tasks"]} == {False, True}


def test_list_tasks_schema_exposes_closed_filter(task_registry: InternalToolRegistry) -> None:
    schema = task_registry.get_schema("list_tasks")

    assert schema is not None
    assert schema["inputSchema"]["properties"]["closed"] == {
        "type": "boolean",
        "description": "true = closed only, false = open only, omitted = both",
    }
