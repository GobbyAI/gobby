"""Active claims accumulate; claim_task explicitly selects the edit target."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_three_active_claims_and_explicit_return_selection(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    tasks = [
        manager.create_task(
            sample_project["id"],
            title,
            validation_criteria="Multiple active claims retain independent ownership.",
        )
        for title in ("First", "Second", "Third")
    ]
    variables = SessionVariableManager(temp_db)

    with session_context_for_test(canonical_task_session.id):
        for task in tasks:
            claimed = await registry.call("claim_task", {"task_id": task.id})
            assert claimed.get("success") is True, claimed
            assert variables.get_variables(canonical_task_session.id)["active_task_id"] == task.id

        selected = await registry.call("claim_task", {"task_id": tasks[0].id})

    assert selected.get("success") is True, selected
    assert selected["task_id"] == tasks[0].id
    state = variables.get_variables(canonical_task_session.id)
    assert state["active_task_id"] == tasks[0].id
    assert set(state["claimed_tasks"]) == {task.id for task in tasks}
    for task in tasks:
        assert manager.get_task(task.id).claimed_by_session_id == canonical_task_session.id


@pytest.mark.asyncio
async def test_create_and_claim_second_and_third_active_tasks(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    task_ids: list[str] = []

    with session_context_for_test(canonical_task_session.id):
        for title in ("First", "Second", "Third"):
            created = await registry.call(
                "create_task",
                {
                    "title": title,
                    "category": "research",
                    "claim": True,
                    "validation_criteria": "Every created task is owned and selected.",
                },
            )
            assert "error" not in created, created
            task_ids.append(created["id"])

    state = SessionVariableManager(temp_db).get_variables(canonical_task_session.id)
    assert set(state["claimed_tasks"]) == set(task_ids)
    assert state["active_task_id"] == task_ids[-1]
    for task_id in task_ids:
        assert manager.get_task(task_id).claimed_by_session_id == canonical_task_session.id


def test_claim_selection_and_release_are_persisted_in_one_history(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    first = manager.create_task(
        sample_project["id"], "First", validation_criteria="Selection survives a release."
    )
    second = manager.create_task(
        sample_project["id"], "Second", validation_criteria="Selection survives a release."
    )
    variables = SessionVariableManager(temp_db)

    manager.claim_task_for_agent(first.id, canonical_task_session.id)
    manager.claim_task_for_agent(second.id, canonical_task_session.id)
    variables.release_task_claim(canonical_task_session.id, second.id)

    state = variables.get_variables(canonical_task_session.id)
    assert state["active_task_id"] == first.id
    history = state["task_selection_history"]
    assert [entry["task_id"] for entry in history] == [first.id, second.id, first.id]
    epochs = [datetime.fromisoformat(entry["epoch"]) for entry in history]
    assert epochs == sorted(epochs)
    assert all(epoch.tzinfo is not None for epoch in epochs)


def test_concurrent_claims_keep_all_owners_and_select_in_commit_order(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    tasks = [
        manager.create_task(
            sample_project["id"],
            str(index),
            validation_criteria="Concurrent selections follow serialized commits.",
        )
        for index in range(3)
    ]

    def claim(task_id: str) -> None:
        LocalTaskManager(temp_db).claim_task_for_agent(task_id, canonical_task_session.id)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(claim, [task.id for task in tasks]))

    state = SessionVariableManager(temp_db).get_variables(canonical_task_session.id)
    history = state["task_selection_history"]
    assert {entry["task_id"] for entry in history} == {task.id for task in tasks}
    assert state["active_task_id"] == history[-1]["task_id"]
    assert set(state["claimed_tasks"]) == {task.id for task in tasks}
    committed_last = max(tasks, key=lambda task: manager.get_task(task.id).updated_at)
    assert state["active_task_id"] == committed_last.id


def test_delayed_edit_uses_its_start_selection_after_focus_changes(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    first = manager.create_task(
        sample_project["id"], "First", validation_criteria="Delayed edits preserve their task."
    )
    second = manager.create_task(
        sample_project["id"], "Second", validation_criteria="Delayed edits preserve their task."
    )
    variables = SessionVariableManager(temp_db)
    manager.claim_task_for_agent(first.id, canonical_task_session.id)
    history = variables.get_variables(canonical_task_session.id)["task_selection_history"]
    started_at = datetime.fromisoformat(history[-1]["epoch"]).timestamp()
    manager.claim_task_for_agent(second.id, canonical_task_session.id)

    recorded = variables.record_edited_files(
        canonical_task_session.id, ["src/a.py"], started_at=started_at
    )

    assert recorded is True
    state = variables.get_variables(canonical_task_session.id)
    assert state["active_task_id"] == second.id
    assert state["task_edited_files"] == {first.id: ["src/a.py"]}


def test_unbound_replay_refuses_attribution_and_names_selection_recovery(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Selected", validation_criteria="Unbound replay never guesses."
    )
    manager.claim_task_for_agent(task.id, canonical_task_session.id)
    variables = SessionVariableManager(temp_db)
    before = variables.get_variables(canonical_task_session.id)
    started_at = (
        datetime.fromisoformat(before["task_selection_history"][0]["epoch"]).timestamp() - 1
    )

    recorded = variables.record_edited_files(
        canonical_task_session.id, ["src/unbound.py"], started_at=started_at
    )

    assert recorded is False
    assert variables.get_variables(canonical_task_session.id) == before
    assert "claim_task" in caplog.text
