"""Compact fleet snapshots resolve standing seats' current persisted claims."""

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_compact_snapshot_joins_current_claims_for_entire_fleet(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    parent = session_manager.register(
        external_id="compact-parent",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    storage = LocalAgentRunManager(temp_db)
    tasks = LocalTaskManager(temp_db)
    expected: dict[str, tuple[str, str]] = {}
    claimed_at = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
    for number in range(42):
        child = session_manager.register(
            external_id=f"compact-child-{number}",
            machine_id=require_machine_id(),
            source="codex",
            project_id=sample_project["id"],
            parent_session_id=parent.id,
        )
        run = storage.create(
            parent_session_id=parent.id,
            child_session_id=child.id,
            provider="codex",
            prompt="standing seat",
            agent_name="developer",
        )
        storage.start(run.id)
        task = tasks.create_task(
            sample_project["id"],
            f"Current work {number}",
            validation_criteria="Current claim appears in the seat snapshot.",
        )
        tasks.claim_task(task.id, child.id)
        temp_db.execute(
            "UPDATE session_tasks SET created_at = %s WHERE session_id = %s AND action = 'claimed'",
            (claimed_at, child.id),
        )
        expected[f"#{child.seq_num}"] = (f"#{task.seq_num}", task.title)
    runner = MagicMock()
    runner.run_storage = storage
    registry = create_agents_registry(runner, session_manager=session_manager, db=temp_db)
    result = await registry._tools["list_running_agents"].func(
        parent_session_id=parent.id,
        view="compact",
    )
    assert result["success"] is True
    assert result["count"] == 42
    assert result["next_offset"] is None
    assert len(json.dumps(result, default=str).encode()) < 15_000
    rows = [dict(zip(result["columns"], row, strict=True)) for row in result["rows"]]
    assert {row["session"] for row in rows} == set(expected)
    for row in rows:
        ref, title = expected[row["session"]]
        assert row["claims"] == [[ref, title, claimed_at.isoformat(), ref[1:]]]

    # Long real task titles force current-snapshot paging without losing a seat.
    temp_db.execute(
        "UPDATE tasks SET title = title || %s WHERE project_id = %s",
        (" long title" * 30, sample_project["id"]),
    )
    offset = 0
    seen: list[str] = []
    while True:
        page = await registry._tools["list_running_agents"].func(
            parent_session_id=parent.id,
            view="compact",
            offset=offset,
        )
        assert len(json.dumps(page, default=str, indent=2).encode()) < 15_000
        seen.extend(row[0] for row in page["rows"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(seen) == 42
    assert set(seen) == set(expected)


@pytest.mark.asyncio
async def test_compact_snapshot_drops_transferred_claim_and_stale_spawn_binding(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    sessions = [
        session_manager.register(
            external_id=f"transfer-{number}",
            machine_id=require_machine_id(),
            source="codex",
            project_id=sample_project["id"],
        )
        for number in range(3)
    ]
    storage = LocalAgentRunManager(temp_db)
    tasks = LocalTaskManager(temp_db)
    old = tasks.create_task(
        sample_project["id"],
        "Transferred task",
        validation_criteria="Transferred ownership replaces the spawn binding.",
    )
    tasks.claim_task(old.id, sessions[1].id)
    tasks.claim_task(old.id, sessions[2].id, force=True)
    run = storage.create(
        parent_session_id=sessions[0].id,
        child_session_id=sessions[1].id,
        provider="codex",
        prompt="standing",
        agent_name="developer",
        task_id=old.id,
    )
    storage.start(run.id)
    runner = MagicMock()
    runner.run_storage = storage
    registry = create_agents_registry(runner, session_manager=session_manager, db=temp_db)
    result = await registry._tools["list_running_agents"].func(
        parent_session_id=sessions[0].id,
        view="compact",
    )
    row = dict(zip(result["columns"], result["rows"][0], strict=True))
    assert row["claims"] == []
    assert result["claims_source"] == "current task owner; first recorded session claim"
