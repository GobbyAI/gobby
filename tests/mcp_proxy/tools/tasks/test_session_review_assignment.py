"""Review assignments belong to every seat that gives a candidate verdict (#24890)."""

from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._session import create_session_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit
MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    ("agent_name", "records_assignment"),
    [("code-reviewer", True), ("security-reviewer", True), ("developer", False)],
)
async def test_reviewing_assignment_follows_the_verdict_seat_role(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    agent_name: str,
    records_assignment: bool,
) -> None:
    """A Security Reviewer's BOUNCE only counts from a recorded assignment."""
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)
    LocalProjectCheckoutManager(temp_db).register(
        MACHINE_ID, sample_project["id"], str(tmp_path / "checkout")
    )
    tasks = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=tasks)
    owner = ctx.session_manager.register(
        external_id="assignment-owner",
        machine_id=MACHINE_ID,
        source="cli",
        project_id=sample_project["id"],
    )
    seat = ctx.session_manager.register(
        external_id=f"assignment-{agent_name}",
        machine_id=MACHINE_ID,
        source="cli",
        project_id=sample_project["id"],
    )
    LocalAgentRunManager(temp_db).create(
        parent_session_id=owner.id,
        child_session_id=seat.id,
        provider="claude",
        agent_name=agent_name,
        prompt="Review the candidate",
    )
    temp_db.execute(
        "UPDATE agent_runs SET status = 'running' WHERE child_session_id = %s", (seat.id,)
    )
    temp_db.execute(
        "UPDATE sessions SET agent_run_id = (SELECT id FROM agent_runs WHERE child_session_id = %s) "
        "WHERE id = %s",
        (seat.id, seat.id),
    )
    task = tasks.create_task(
        project_id=sample_project["id"],
        title="Reviewed candidate",
        created_in_session_id=owner.id,
        claimed_by_session_id=owner.id,
        validation_criteria="The verdict seat records its assignment.",
    )

    with session_context_for_test(seat.id):
        result = await create_session_registry(ctx).call(
            "link_task_to_session", {"task_id": task.id, "action": "reviewing"}
        )

    recorded = any(
        link["session_id"] == seat.id and link["action"] == "reviewing"
        for link in SessionTaskManager(temp_db).get_task_sessions(task.id)
    )
    assert recorded is records_assignment
    if records_assignment:
        assert result == {}
    else:
        assert result["error_code"] == "TASK_CLAIM_CONFLICT"
