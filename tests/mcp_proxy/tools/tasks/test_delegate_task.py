"""A filer can durably delegate an unclaimed task to a live session."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any
from unittest.mock import patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", "21000000-0000-4000-8000-000000000002"):
        yield


def _setup(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> tuple[SessionManager, Session, Session, Task, InternalToolRegistry]:
    sessions = SessionManager(temp_db)
    filer = sessions.register(
        external_id="delegation-filer",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=sample_project["id"],
    )
    receiver = sessions.register(
        external_id="delegation-receiver",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=sample_project["id"],
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Finding for another lane",
        created_in_session_id=filer.id,
        category="code",
        validation_criteria="The receiver fixes the finding.",
    )
    registry = create_task_registry(manager)
    return sessions, filer, receiver, task, registry


@pytest.mark.asyncio
async def test_delegate_task_records_live_receiver(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    _, filer, receiver, task, registry = _setup(temp_db, sample_project)

    with session_context_for_test(filer.id):
        result = await registry.call(
            "delegate_task",
            {
                "task_id": task.id,
                "delegated_to_session_ref": f"#{receiver.seq_num}",
                "reason": "The receiver owns this lane.",
            },
        )

    assert "error" not in result, result
    assert result["delegated_to_session_ref"] == f"#{receiver.seq_num}"
    assert result["delegated_by_session_id"] == filer.id
    assert result["reason"] == "The receiver owns this lane."
    assert result["delegated_at"]


@pytest.mark.asyncio
async def test_delegate_task_rejects_self(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    _, filer, _, task, registry = _setup(temp_db, sample_project)

    with session_context_for_test(filer.id):
        result = await registry.call(
            "delegate_task",
            {
                "task_id": task.id,
                "delegated_to_session_ref": f"#{filer.seq_num}",
                "reason": "Cannot delegate to self.",
            },
        )

    assert "error" in result
    assert "self" in result["error"].lower()


async def test_delegate_task_requires_filer_authority(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    _, filer, receiver, task, registry = _setup(temp_db, sample_project)

    with session_context_for_test(receiver.id):
        result = await registry.call(
            "delegate_task",
            {
                "task_id": task.id,
                "delegated_to_session_ref": f"#{filer.seq_num}",
                "reason": "Another session may not transfer this finding.",
            },
        )

    assert "Only the session that filed" in result["error"]
    assert LocalTaskManager(temp_db).get_task(task.id).delegated_to_session_id is None


@pytest.mark.asyncio
async def test_delegate_task_rejects_ended_receiver(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions, filer, receiver, task, registry = _setup(temp_db, sample_project)
    sessions.update_status(receiver.id, "expired")

    with session_context_for_test(filer.id):
        result = await registry.call(
            "delegate_task",
            {
                "task_id": task.id,
                "delegated_to_session_ref": f"#{receiver.seq_num}",
                "reason": "Receiver has ended.",
            },
        )

    assert "error" in result
    assert "live" in result["error"].lower()


@pytest.mark.parametrize("filer_status", ["expired", "closed"])
async def test_current_receiver_can_transfer_after_filer_ends(
    temp_db: HubDatabase, sample_project: dict[str, Any], filer_status: str
) -> None:
    sessions, filer, receiver, task, registry = _setup(temp_db, sample_project)
    successor = sessions.register(
        external_id="delegation-successor",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=sample_project["id"],
    )
    with session_context_for_test(filer.id):
        initial = await registry.call(
            "delegate_task",
            {
                "task_id": task.id,
                "delegated_to_session_ref": f"#{receiver.seq_num}",
                "reason": "Initial lane assignment.",
            },
        )
    assert "error" not in initial, initial

    sessions.update_status(filer.id, filer_status)
    with session_context_for_test(receiver.id):
        result = await registry.call(
            "delegate_task",
            {
                "task_id": task.id,
                "delegated_to_session_ref": f"#{successor.seq_num}",
                "reason": "PD approved a transfer after the filer ended.",
            },
        )

    assert "error" not in result, result
    assert result["delegated_to_session_id"] == successor.id
    assert result["delegated_by_session_id"] == receiver.id
    assert result["reason"] == "PD approved a transfer after the filer ended."


@pytest.mark.parametrize(
    "filer_status",
    ["active", "paused", "interrupted", "awaiting_input", "awaiting_approval", "awaiting_handoff"],
)
async def test_current_receiver_cannot_transfer_while_filer_is_live(
    temp_db: HubDatabase, sample_project: dict[str, Any], filer_status: str
) -> None:
    sessions, filer, receiver, task, registry = _setup(temp_db, sample_project)
    successor = sessions.register(
        external_id="delegation-successor",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=sample_project["id"],
    )
    with session_context_for_test(filer.id):
        initial = await registry.call(
            "delegate_task",
            {
                "task_id": task.id,
                "delegated_to_session_ref": f"#{receiver.seq_num}",
                "reason": "Initial lane assignment.",
            },
        )
    assert "error" not in initial, initial
    if filer_status != "active":
        sessions.update_status(filer.id, filer_status)

    with session_context_for_test(receiver.id):
        result = await registry.call(
            "delegate_task",
            {
                "task_id": task.id,
                "delegated_to_session_ref": f"#{successor.seq_num}",
                "reason": "Premature transfer.",
            },
        )

    assert "Only the session that filed" in result["error"]
    assert LocalTaskManager(temp_db).get_task(task.id).delegated_to_session_id == receiver.id


async def test_unrelated_session_cannot_transfer_after_filer_ends(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions, filer, receiver, task, registry = _setup(temp_db, sample_project)
    outsider = sessions.register(
        external_id="delegation-outsider",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=sample_project["id"],
    )
    successor = sessions.register(
        external_id="delegation-successor",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=sample_project["id"],
    )
    with session_context_for_test(filer.id):
        initial = await registry.call(
            "delegate_task",
            {
                "task_id": task.id,
                "delegated_to_session_ref": f"#{receiver.seq_num}",
                "reason": "Initial lane assignment.",
            },
        )
    assert "error" not in initial, initial
    sessions.update_status(filer.id, "expired")

    with session_context_for_test(outsider.id):
        result = await registry.call(
            "delegate_task",
            {
                "task_id": task.id,
                "delegated_to_session_ref": f"#{successor.seq_num}",
                "reason": "Unrelated takeover.",
            },
        )

    assert "error" in result
    assert LocalTaskManager(temp_db).get_task(task.id).delegated_to_session_id == receiver.id


def test_ended_receiver_cannot_transfer_after_filer_ends(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions, filer, receiver, task, _ = _setup(temp_db, sample_project)
    successor = sessions.register(
        external_id="delegation-successor",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=sample_project["id"],
    )
    manager = LocalTaskManager(temp_db)
    manager.delegate_task(
        task.id,
        delegated_by_session_id=filer.id,
        delegated_to_session_id=receiver.id,
        reason="Initial lane assignment.",
    )
    sessions.update_status(filer.id, "expired")
    sessions.update_status(receiver.id, "expired")

    with pytest.raises(ValueError, match="Only the session that filed"):
        manager.delegate_task(
            task.id,
            delegated_by_session_id=receiver.id,
            delegated_to_session_id=successor.id,
            reason="Ended receiver must not transfer.",
        )
    assert manager.get_task(task.id).delegated_to_session_id == receiver.id


def test_current_receiver_cannot_transfer_to_ended_target(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions, filer, receiver, task, _ = _setup(temp_db, sample_project)
    successor = sessions.register(
        external_id="delegation-successor",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=sample_project["id"],
    )
    manager = LocalTaskManager(temp_db)
    manager.delegate_task(
        task.id,
        delegated_by_session_id=filer.id,
        delegated_to_session_id=receiver.id,
        reason="Initial lane assignment.",
    )
    sessions.update_status(filer.id, "expired")
    sessions.update_status(successor.id, "expired")

    with pytest.raises(ValueError, match="live receiving session"):
        manager.delegate_task(
            task.id,
            delegated_by_session_id=receiver.id,
            delegated_to_session_id=successor.id,
            reason="Ended target must not receive.",
        )
    assert manager.get_task(task.id).delegated_to_session_id == receiver.id


def test_competing_receiver_transfers_have_one_winner(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions, filer, receiver, task, _ = _setup(temp_db, sample_project)
    successors = [
        sessions.register(
            external_id=f"delegation-successor-{index}",
            machine_id="21000000-0000-4000-8000-000000000002",
            source="codex",
            project_id=sample_project["id"],
        )
        for index in range(2)
    ]
    manager = LocalTaskManager(temp_db)
    manager.delegate_task(
        task.id,
        delegated_by_session_id=filer.id,
        delegated_to_session_id=receiver.id,
        reason="Initial lane assignment.",
    )
    sessions.update_status(filer.id, "expired")
    barrier = Barrier(2)

    def transfer(successor: Session) -> Task | str:
        barrier.wait()
        try:
            return LocalTaskManager(temp_db).delegate_task(
                task.id,
                delegated_by_session_id=receiver.id,
                delegated_to_session_id=successor.id,
                reason=f"Transfer to {successor.seq_num}.",
            )
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(transfer, successors))

    assert sum(isinstance(outcome, Task) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, str) for outcome in outcomes) == 1
    winning_task = next(outcome for outcome in outcomes if isinstance(outcome, Task))
    assert manager.get_task(task.id).delegated_to_session_id == winning_task.delegated_to_session_id
    assert winning_task.delegated_to_session_id in {successor.id for successor in successors}
