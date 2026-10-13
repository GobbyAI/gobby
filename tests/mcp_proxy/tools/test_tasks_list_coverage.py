"""Regression coverage for MCP task-list filters and counts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.close_receipts import INDEPENDENT_REVIEW_APPROVAL, record_close_receipt
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

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
    event = (
        f"LANE=1 EVENT=CANDIDATE TASK=#{candidate.seq_num} TASK_TITLE=Waiting RUN=run "
        f"NOTE=quotes TASK=#{linked.seq_num}"
    )
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
    with patch.object(temp_db, "fetchall", wraps=temp_db.fetchall) as fetchall:
        times = manager.get_candidate_sent_times([candidate.id, linked.id])
    assert times == {candidate.id: first.sent_at}
    statement, params = fetchall.call_args.args
    plan = temp_db.fetchall("EXPLAIN " + statement, params)
    assert "SubPlan" not in "\n".join(str(row["QUERY PLAN"]) for row in plan)


def test_candidate_queries_group_extraction_before_joining_tasks(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    manager = LocalTaskManager(temp_db)
    seat = session_manager.register(
        external_id="candidate-plan-author",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    tasks = [
        manager.create_task(
            sample_project["id"],
            f"Candidate plan item {number}",
            validation_criteria="Candidate filtering extracts each event before task joins.",
        )
        for number in range(4)
    ]
    messages = InterSessionMessageManager(temp_db)
    for task in tasks[:2]:
        messages.create_message(seat.id, seat.id, f"EVENT=CANDIDATE TASK=#{task.seq_num}")
    with patch.object(temp_db, "fetchall", wraps=temp_db.fetchall) as fetchall:
        times = manager.get_candidate_sent_times([task.id for task in tasks])
        candidates = manager.list_tasks(
            project_id=sample_project["id"], candidate=True, closed=False, sort_by="created_at"
        )
        others = manager.list_tasks(
            project_id=sample_project["id"], candidate=False, closed=False, sort_by="created_at"
        )
    assert set(times) == {task.id for task in tasks[:2]}
    assert {task.id for task in candidates} == {task.id for task in tasks[:2]}
    assert {task.id for task in others} == {task.id for task in tasks[2:]}
    statements = [
        (call.args[0], call.args[1])
        for call in fetchall.call_args_list
        if "inter_session_messages" in call.args[0]
    ]
    assert len(statements) == 3
    for statement, params in statements:
        lines = [str(row["QUERY PLAN"]) for row in temp_db.fetchall("EXPLAIN " + statement, params)]
        plan = "\n".join(lines)
        assert "SubPlan" not in plan, plan
        assert any("Group Key" in line and "substring" in line.lower() for line in lines), plan


@pytest.mark.asyncio
async def test_list_tasks_caps_only_compact_view() -> None:
    manager = MagicMock()
    manager.list_tasks.return_value = []
    manager.count_tasks.return_value = 0
    manager.get_candidate_sent_times.return_value = {}
    registry = create_task_registry(manager, MagicMock())
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as context:
        context.return_value = {"id": "project"}
        await registry.call("list_tasks", {"limit": 500})
        assert manager.list_tasks.call_args.kwargs["limit"] == 500
        await registry.call("list_tasks", {"limit": 500, "view": "compact"})
        assert manager.list_tasks.call_args.kwargs["limit"] == 100


@pytest.mark.asyncio
async def test_list_tasks_reports_bad_refs_without_losing_valid_rows(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    tasks = [
        manager.create_task(
            sample_project["id"],
            f"Valid queue item {number}",
            validation_criteria="Partial batches preserve order and report missing refs.",
        )
        for number in range(2)
    ]
    refs = [f"#{tasks[0].seq_num}", "missing-task", f"#{tasks[1].seq_num}"]
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as context:
        context.return_value = {"id": sample_project["id"]}
        first = await registry.call("list_tasks", {"refs": refs, "view": "compact", "limit": 2})
        second = await registry.call(
            "list_tasks",
            {"refs": refs, "view": "compact", "limit": 2, "offset": first["next_offset"]},
        )
    assert first["count"] == second["count"] == 1
    assert first["next_offset"] == 2
    assert second["next_offset"] is None
    assert first["rows"][0][0] == refs[0]
    assert second["rows"][0][0] == refs[2]
    assert first["ref_errors"][0]["ref"] == "missing-task"
    assert first["ref_errors"][0]["offset"] == 1
    assert "error" in first["ref_errors"][0]


@pytest.mark.asyncio
async def test_compact_ref_page_budget_preserves_source_offsets(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    tasks = [
        manager.create_task(
            sample_project["id"],
            f"Long queue item {number} " + "x" * 6_000,
            validation_criteria="Byte-limited ref pages preserve remaining task positions.",
        )
        for number in range(3)
    ]
    refs = ["missing-task", *(f"#{task.seq_num}" for task in tasks), "missing-end"]
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as context:
        context.return_value = {"id": sample_project["id"]}
        first = await registry.call("list_tasks", {"refs": refs, "view": "compact"})
        second = await registry.call(
            "list_tasks",
            {"refs": refs, "view": "compact", "offset": first["next_offset"]},
        )
    assert first["count"] == 2
    assert first["next_offset"] == 3
    assert second["count"] == 1
    assert second["next_offset"] is None
    assert [row[0] for page in (first, second) for row in page["rows"]] == refs[1:-1]
    assert [error["offset"] for error in first["ref_errors"]] == [0]
    assert [error["offset"] for error in second["ref_errors"]] == [4]
    assert all(
        len(json.dumps(page, default=str, indent=2).encode()) < 15_000 for page in (first, second)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized_position", [0, 1, 2])
async def test_compact_page_elides_oversized_row_without_losing_neighbors(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    oversized_position: int,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    tasks = [
        manager.create_task(
            sample_project["id"],
            "Oversized " + "x" * 16_000 if number == oversized_position else f"Queue {number}",
            validation_criteria="An oversized row must not prevent reading neighboring tasks.",
        )
        for number in range(3)
    ]
    refs = ["missing-task", *(f"#{task.seq_num}" for task in tasks)]
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as context:
        context.return_value = {"id": sample_project["id"]}
        result = await registry.call("list_tasks", {"refs": refs, "view": "compact"})

    assert "error" not in result
    assert result["count"] == 3
    assert result["next_offset"] is None
    rows = [dict(zip(result["columns"], row, strict=True)) for row in result["rows"]]
    assert [row["ref"] for row in rows] == refs[1:]
    for number, row in enumerate(rows):
        if number == oversized_position:
            assert "elided" in row["title"]
            assert "get_task" in row["title"]
            assert tasks[number].id in row["title"]
            assert "brief=false" in row["title"]
        else:
            assert row["title"] == tasks[number].title
    assert [(error["ref"], error["offset"]) for error in result["ref_errors"]] == [
        ("missing-task", 0)
    ]
    assert len(json.dumps(result, default=str, indent=2).encode()) < 14_000


@pytest.mark.asyncio
@pytest.mark.parametrize("fact_size", [1, 300])
async def test_compact_receipt_facts_preserve_summaries_and_full_fetch(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    fact_size: int,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    seat = session_manager.register(
        external_id="receipt-author",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    tasks = [
        manager.create_task(
            sample_project["id"],
            f"Queue {number}",
            validation_criteria="Receipt summaries remain readable and full facts are retrievable.",
        )
        for number in range(3)
    ]
    oversized = tasks[1]
    facts = {f"fact{number}": "x" * fact_size for number in range(16)}
    receipts = [
        record_close_receipt(
            temp_db,
            task=oversized,
            author_session_id=seat.id,
            kind=INDEPENDENT_REVIEW_APPROVAL,
            commit_sha=f"{number:040x}",
            facts=facts,
        )[0]
        for number in range(4)
    ]
    sent = InterSessionMessageManager(temp_db).create_message(
        seat.id, seat.id, f"EVENT=CANDIDATE TASK=#{oversized.seq_num} TASK_TITLE=Queue"
    )
    refs = [f"#{task.seq_num}" for task in tasks]
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as context:
        context.return_value = {"id": sample_project["id"]}
        page = await registry.call("list_tasks", {"refs": refs, "view": "compact"})
        with patch(
            "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
            return_value={"id": sample_project["id"]},
        ):
            full = await registry.call("get_task", {"task_id": refs[1], "brief": False})

    rows = [dict(zip(page["columns"], row, strict=True)) for row in page["rows"]]
    assert [row["ref"] for row in rows] == refs
    assert [row["title"] for row in rows] == [task.title for task in tasks]
    assert rows[1]["candidate_sent_at"] == sent.sent_at.isoformat()
    assert [receipt[:3] for receipt in rows[1]["receipts"]] == [
        [receipt.kind, receipt.commit_sha, receipt.recorded_at] for receipt in receipts
    ]
    assert all("get_task" in receipt[3]["_elided"] for receipt in rows[1]["receipts"])
    assert full["candidate_sent_at"] == sent.sent_at.isoformat()
    assert full["receipts"] == [
        [receipt.kind, receipt.commit_sha, receipt.recorded_at, facts] for receipt in receipts
    ]
    assert len(json.dumps(page, default=str, indent=2).encode()) < 14_000


@pytest.mark.asyncio
async def test_compact_cross_project_pointer_fetches_the_originating_task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    other_project = install_isolated_checkout_project(
        temp_db, tmp_path / "other-project", name="other-project", machine_id=require_machine_id()
    ).project
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    caller_task = manager.create_task(
        sample_project["id"], "Caller task", validation_criteria="Belongs to the caller project."
    )
    other_task = manager.create_task(
        other_project.id,
        "Other task " + "x" * 16_000,
        validation_criteria="Full pointer fetch stays in its project.",
    )
    assert caller_task.seq_num == other_task.seq_num
    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._context.get_project_context",
            return_value={"id": sample_project["id"]},
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
            return_value={"id": sample_project["id"]},
        ),
    ):
        page = await registry.call("list_tasks", {"view": "compact", "all_projects": True})
        rows = [dict(zip(page["columns"], row, strict=True)) for row in page["rows"]]
        other_row = next(row for row in rows if "elided" in row["title"])
        pointer = other_row["title"]
        fetch_id = pointer.split("task_id='", 1)[1].split("'", 1)[0]
        full = await registry.call("get_task", {"task_id": fetch_id, "brief": False})
    assert full["id"] == other_task.id
    assert fetch_id == other_task.id
    assert other_row["ref"] == f"#{other_task.seq_num}"
    assert full["title"] == other_task.title


@pytest.mark.asyncio
async def test_compact_first_row_degrades_to_fit_beside_ref_errors(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    task = manager.create_task(
        sample_project["id"],
        "Mid-size " + "x" * 7_000,
        validation_criteria="Ref errors do not prevent reading the first task row.",
    )
    ref = f"#{task.seq_num}"
    refs = [*(f"missing-{number}" for number in range(65)), ref]
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as context:
        context.return_value = {"id": sample_project["id"]}
        alone = await registry.call("list_tasks", {"refs": [ref], "view": "compact"})
        page = await registry.call("list_tasks", {"refs": refs, "view": "compact", "limit": 100})
    assert alone["rows"][0][1] == task.title
    assert "error" not in page
    assert page["rows"][0][0] == ref
    assert page["rows"][0][3] == task.task_type
    assert "get_task" in page["rows"][0][1]
    assert len(page["ref_errors"]) == 65
    assert len(json.dumps(page, default=str, indent=2).encode()) < 14_000


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


@pytest.mark.asyncio
@pytest.mark.parametrize("filter_name", ["claimed", "escalated", "candidate", "updated_since"])
async def test_compact_totals_match_filters_on_every_page(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    filter_name: str,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager, MagicMock())
    seat = session_manager.register(
        external_id="total-author",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    tasks = [
        manager.create_task(
            sample_project["id"],
            f"Total item {number}",
            validation_criteria="Filtered totals equal all matching rows.",
        )
        for number in range(4)
    ]
    for index, task in enumerate(tasks):
        temp_db.execute(
            "UPDATE tasks SET claimed_by_session_id = %s, is_escalated = %s, updated_at = %s "
            "WHERE id = %s",
            (
                seat.id if index < 2 else None,
                index < 2,
                datetime(2026, 10, 12 if index < 2 else 10, tzinfo=UTC),
                task.id,
            ),
        )
    messages = InterSessionMessageManager(temp_db)
    for task in tasks[:2]:
        event = f"EVENT=CANDIDATE TASK=#{task.seq_num} TASK_TITLE=Total"
        messages.create_message(seat.id, seat.id, event)
        messages.create_message(seat.id, seat.id, event)
    filters: dict[str, Any] = {
        filter_name: "2026-10-11T00:00:00Z" if filter_name == "updated_since" else True
    }
    with patch(
        "gobby.mcp_proxy.tools.tasks._context.get_project_context",
        return_value={"id": sample_project["id"]},
    ):
        pages = [
            await registry.call(
                "list_tasks", {**filters, "view": "compact", "limit": 1, "offset": offset}
            )
            for offset in range(3)
        ]
    assert [page["total"] for page in pages] == [2, 2, 2]
    assert [page["count"] for page in pages] == [1, 1, 0]
    assert [page["next_offset"] for page in pages] == [1, None, None]
    assert {page["rows"][0][0] for page in pages[:2]} == {f"#{task.seq_num}" for task in tasks[:2]}
    if filter_name != "updated_since":
        negative_filters: dict[str, Any] = {filter_name: False}
        assert manager.count_tasks(project_id=sample_project["id"], **negative_filters) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_filter", [True, False])
async def test_blocked_compact_pages_have_exact_total_and_blocker_refs(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    parent_filter: bool,
) -> None:
    from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry
    from gobby.storage.task_dependencies import TaskDependencyManager

    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(sample_project["id"], "Parent", validation_criteria="Control")
    blocker = manager.create_task(sample_project["id"], "Blocker", validation_criteria="Control")
    tasks = [
        manager.create_task(
            sample_project["id"],
            f"Blocked {index}",
            validation_criteria="Blocked pages retain references",
            parent_task_id=parent.id if index < 2 else None,
        )
        for index in range(3)
    ]
    dependencies = TaskDependencyManager(temp_db)
    for task in tasks:
        dependencies.add_dependency(task.id, blocker.id, "blocks")
    registry = create_readiness_registry(manager)
    expected = tasks[:2] if parent_filter else tasks
    filters = {"parent_task_id": f"#{parent.seq_num}"} if parent_filter else {}
    with patch(
        "gobby.mcp_proxy.tools.tasks._context.get_project_context",
        return_value={"id": sample_project["id"]},
    ):
        pages = [
            await registry.call(
                "list_blocked_tasks", {**filters, "view": "compact", "limit": 1, "offset": offset}
            )
            for offset in range(len(expected) + 1)
        ]
    assert [page["total"] for page in pages] == [len(expected)] * len(pages)
    assert [page["next_offset"] for page in pages] == [*range(1, len(expected)), None, None]
    assert [page["count"] for page in pages] == [1] * len(expected) + [0]
    assert {page["rows"][0][0] for page in pages[:-1]} == {f"#{task.seq_num}" for task in expected}
    assert all(page["rows"][0][8] == [[f"#{blocker.seq_num}", "Blocker"]] for page in pages[:-1])
    manager.close_task(blocker.id)
    assert manager.count_blocked_tasks(project_id=sample_project["id"]) == 0


def test_count_title_matches_literal_list_filter(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    expected = manager.create_task(sample_project["id"], "100%_done", validation_criteria="Control")
    manager.create_task(sample_project["id"], "100abcXdone", validation_criteria="Control")
    filters = {"project_id": sample_project["id"], "title_like": "%_"}
    assert [task.id for task in manager.list_tasks(**filters)] == [expected.id]
    assert manager.count_tasks(**filters) == 1
