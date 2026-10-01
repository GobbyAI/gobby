"""An escalated task the caller still holds is released by escalate_task (#23204).

Escalated plus claimed is a legitimate state: an operator claims an escalated task
to close it deliberately with override_justification. The single-claim rule still
counts that row as the session's open claim, so escalate_task must release it.
"""

from __future__ import annotations

from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._errors import TaskToolErrorCode
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


def _task(manager: LocalTaskManager, project: dict[str, Any], title: str) -> Task:
    return manager.create_task(
        project["id"],
        title,
        validation_criteria="The escalated claim behavior is observable.",
    )


async def _call(registry: Any, session: Session, tool: str, **arguments: Any) -> dict[str, Any]:
    with session_context_for_test(session.id):
        result: dict[str, Any] = await registry.call(tool, arguments)
    return result


async def _hold_escalated(
    registry: Any, manager: LocalTaskManager, session: Session, task: Task
) -> Task:
    """Claim an already-escalated task, the supported override-close setup."""
    manager.escalate_task(task.id, reason="needs a human decision")
    claimed = await _call(registry, session, "claim_task", task_id=task.id)
    assert claimed.get("success") is True, claimed
    held = manager.get_task(task.id)
    assert held.is_escalated is True
    assert held.claimed_by_session_id == session.id
    return held


@pytest.mark.asyncio
async def test_escalate_task_releases_an_escalated_task_the_caller_holds(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    held = await _hold_escalated(
        registry, manager, canonical_task_session, _task(manager, sample_project, "Held")
    )

    result = await _call(
        registry, canonical_task_session, "escalate_task", task_id=held.id, reason="parking"
    )

    assert result == {}
    released = manager.get_task(held.id)
    assert released.claimed_by_session_id is None
    # The release is not a new escalation: the original metadata stays.
    assert released.is_escalated is True
    assert released.escalated_at == held.escalated_at
    assert released.escalation_reason == "needs a human decision"
    variables = SessionVariableManager(temp_db).get_variables(canonical_task_session.id)
    assert variables["claimed_tasks"] == {}
    assert variables["task_claimed"] is False


@pytest.mark.asyncio
async def test_released_session_can_claim_another_task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    held = await _hold_escalated(
        registry, manager, canonical_task_session, _task(manager, sample_project, "Held")
    )
    other = _task(manager, sample_project, "Next")
    await _call(registry, canonical_task_session, "escalate_task", task_id=held.id, reason="park")

    claimed = await _call(registry, canonical_task_session, "claim_task", task_id=other.id)

    assert claimed.get("success") is True, claimed
    assert manager.get_task(other.id).claimed_by_session_id == canonical_task_session.id


@pytest.mark.asyncio
async def test_claim_conflict_on_a_held_escalated_task_names_the_working_exits(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    held = await _hold_escalated(
        registry, manager, canonical_task_session, _task(manager, sample_project, "Held")
    )
    other = _task(manager, sample_project, "Next")

    refused = await _call(registry, canonical_task_session, "claim_task", task_id=other.id)

    assert refused["error_code"] == TaskToolErrorCode.TASK_CLAIM_CONFLICT.value
    held_ref = f"#{held.seq_num}"
    assert f'escalate_task(task_id="{held_ref}"' in refused["error"]
    assert "already escalated" in refused["error"]
    assert "override_justification" in refused["error"]
    assert manager.get_task(other.id).claimed_by_session_id is None


@pytest.mark.asyncio
async def test_claim_conflict_on_an_open_claim_keeps_its_guidance(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    held = _task(manager, sample_project, "Held")
    other = _task(manager, sample_project, "Next")
    await _call(registry, canonical_task_session, "claim_task", task_id=held.id)

    refused = await _call(registry, canonical_task_session, "claim_task", task_id=other.id)

    assert refused["error_code"] == TaskToolErrorCode.TASK_CLAIM_CONFLICT.value
    assert f"Session already owns open claimed task #{held.seq_num}." in refused["error"]
    assert "already escalated" not in refused["error"]
    assert "override_justification" not in refused["error"]


@pytest.mark.asyncio
async def test_escalate_task_on_an_unheld_escalated_task_names_de_escalate(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    task = _task(manager, sample_project, "Parked")
    escalated = manager.escalate_task(task.id, reason="needs a human decision")

    refused = await _call(
        registry, canonical_task_session, "escalate_task", task_id=task.id, reason="again"
    )

    assert refused["error_code"] == TaskToolErrorCode.TASK_INVALID_STATUS.value
    assert "de_escalate_task" in refused["error"]
    unchanged = manager.get_task(task.id)
    assert unchanged.escalated_at == escalated.escalated_at
    assert unchanged.escalation_reason == "needs a human decision"


@pytest.mark.asyncio
async def test_ordinary_escalate_de_escalate_reclaim_cycle_is_unchanged(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
) -> None:
    manager = LocalTaskManager(temp_db)
    registry = create_task_registry(manager)
    task = _task(manager, sample_project, "Cycle")
    session = canonical_task_session

    await _call(registry, session, "claim_task", task_id=task.id)
    escalated = await _call(registry, session, "escalate_task", task_id=task.id, reason="park")
    parked = manager.get_task(task.id)
    de_escalated = await _call(
        registry, session, "de_escalate_task", task_id=task.id, reason="resolved"
    )
    reclaimed = await _call(registry, session, "claim_task", task_id=task.id)

    assert escalated == {}
    assert parked.is_escalated is True
    assert parked.claimed_by_session_id is None
    assert de_escalated.get("error") is None, de_escalated
    assert reclaimed.get("success") is True, reclaimed
    final = manager.get_task(task.id)
    assert final.is_escalated is False
    assert final.claimed_by_session_id == session.id
