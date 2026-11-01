"""Historical workflow metadata has no built-in lifecycle authority."""

from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("action", ["reopen", "de_escalate", "mcp_de_escalate"])
async def test_review_counters_neither_block_lifecycle_nor_trigger_automatic_resets(
    temp_db: HubDatabase, sample_project: dict[str, Any], action: str
) -> None:
    author = AgentSessions(temp_db, sample_project["id"]).add("ordinary-worker")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Resolve explicit escalation")
    manager.claim_task(task.id, author)
    temp_db.execute(
        "UPDATE tasks SET review_fail_count = 3, review_epoch = 7, review_status = 'invalid', "
        "escalated_at = NOW(), escalation_reason = 'Explicit escalation' WHERE id = %s",
        (task.id,),
    )
    if action == "reopen":
        manager.reopen_task(task.id, reason="The owner resumes work.")
    elif action == "de_escalate":
        manager.de_escalate_task(task.id, reason="The owner resolved the escalation.")
    else:
        with session_context_for_test(author):
            result = await create_task_registry(manager).call(
                "de_escalate_task",
                {"task_id": task.id, "reason": "The owner resolved the escalation."},
            )
        assert "error" not in result, result
    resolved = manager.get_task(task.id)
    assert resolved.is_escalated is False
    assert resolved.review_fail_count == 3
    assert resolved.review_epoch == 7
    assert resolved.review_status == "invalid"
    assert resolved.claimed_by_session_id == (None if action == "reopen" else author)
