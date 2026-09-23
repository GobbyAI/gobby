"""A filer can durably delegate an unclaimed task to a live session."""

from __future__ import annotations

from collections.abc import Iterator
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
